import os
import logging
import uuid
import time
from urllib.parse import urljoin
import requests
from flask import Flask, request, jsonify, send_file, render_template, flash, redirect, url_for, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather, Play
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Create Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-key-change-in-production")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create audio directory if it doesn't exist
AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_files')
os.makedirs(AUDIO_DIR, exist_ok=True)

# Load configuration from environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
SERVER_URL = os.getenv("SERVER_URL")

# Global conversation storage (in production, use Redis or database)
conversations = {}

# Initialize Twilio client
twilio_client = None
try:
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio client initialized successfully")
    else:
        logger.warning("Twilio credentials not found in environment variables")
except Exception as e:
    logger.error(f"Twilio initialization failed: {e}")

def detect_language(text):
    """Enhanced language detection for Indian languages"""
    # Telugu script detection
    if any('\u0C00' <= c <= '\u0C7F' for c in text):
        return "te"
    # Hindi script detection (Devanagari)
    if any('\u0900' <= c <= '\u097F' for c in text):
        return "hi"
    # Tamil script detection
    if any('\u0B80' <= c <= '\u0BFF' for c in text):
        return "ta"
    # Kannada script detection
    if any('\u0C80' <= c <= '\u0CFF' for c in text):
        return "kn"
    # Malayalam script detection
    if any('\u0D00' <= c <= '\u0D7F' for c in text):
        return "ml"
    # Gujarati script detection
    if any('\u0A80' <= c <= '\u0AFF' for c in text):
        return "gu"
    # Bengali script detection
    if any('\u0980' <= c <= '\u09FF' for c in text):
        return "bn"
    # Marathi uses Devanagari, so check for specific Marathi words or patterns
    # Punjabi (Gurmukhi script) detection
    if any('\u0A00' <= c <= '\u0A7F' for c in text):
        return "pa"
    # Odia script detection
    if any('\u0B00' <= c <= '\u0B7F' for c in text):
        return "od"
    # Default to English
    return "en"

def get_llm_response(user_input, conversation_history, system_message):
    """Get response from OpenRouter LLM"""
    logger.info(f"Getting LLM response for: {user_input[:50]}...")
    
    if not OPENROUTER_API_KEY:
        logger.error("OpenRouter API Key not configured.")
        return "Error: LLM not configured."
    
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        messages = [{"role": "system", "content": system_message}]
        
        # Keep context limited to last 6 messages
        messages.extend(conversation_history[-6:])
        messages.append({"role": "user", "content": user_input})
        
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": "openai/gpt-3.5-turbo",
            "messages": messages,
            "max_tokens": 30,  # Ultra-short responses for minimal latency
            "temperature": 0.3,  # More focused responses
            "stream": False
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=5)
        
        if response.status_code == 200:
            result = response.json()
            ai_response = result['choices'][0]['message']['content'].strip()
            logger.info(f"LLM response: {ai_response[:50]}...")
            return ai_response
        else:
            logger.error(f"LLM API error: {response.status_code} - {response.text}")
            return "I encountered an issue generating a response."
            
    except Exception as e:
        logger.error(f"LLM request error: {e}")
        return "I had trouble connecting to the AI model."

def generate_sarvam_tts(text, language="hi", voice="female"):
    """Generate TTS audio using Sarvam AI Bulbul model"""
    logger.info(f"Generating Sarvam TTS for: '{text[:50]}...' in {language}")
    
    if not SARVAM_API_KEY:
        logger.error("Sarvam AI API Key not configured.")
        return None
        
    if not SERVER_URL:
        logger.error("SERVER_URL not configured. Cannot create audio URL.")
        return None
    
    # Map language codes to Sarvam AI format
    language_map = {
        "hi": "hi-IN",
        "en": "en-IN",
        "bn": "bn-IN",
        "gu": "gu-IN",
        "kn": "kn-IN",
        "ml": "ml-IN",
        "mr": "mr-IN",
        "od": "od-IN",
        "pa": "pa-IN",
        "ta": "ta-IN",
        "te": "te-IN"
    }
    
    # Map voice preferences to Sarvam AI speakers (bulbul:v2 compatible)
    voice_map = {
        "female": "anushka",
        "male": "abhilash",
        "abhilash": "abhilash",
        "anushka": "anushka",
        "manisha": "manisha",
        "vidya": "vidya",
        "arya": "arya",
        "karun": "karun",
        "hitesh": "hitesh"
    }
    
    target_language = language_map.get(language, "en-IN")
    speaker = voice_map.get(voice, "anushka")
    
    try:
        api_url = "https://api.sarvam.ai/text-to-speech"
        filename = f"tts_{int(time.time())}_{uuid.uuid4().hex[:8]}.wav"
        output_path = os.path.join(AUDIO_DIR, filename)
        
        payload = {
            "inputs": [text[:200]],  # Shorter text for faster TTS generation
            "target_language_code": target_language,
            "speaker": speaker,
            "model": "bulbul:v2",
            "audio_format": "wav"  # Explicitly request WAV format for Twilio compatibility
        }
        
        headers = {
            "api-subscription-key": SARVAM_API_KEY,
            "Content-Type": "application/json"
        }
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=8)
        
        if response.status_code == 200:
            # Handle both JSON response (with base64 audio) and direct binary response
            try:
                response_data = response.json()
                if 'audios' in response_data and len(response_data['audios']) > 0:
                    # New API format returns base64 encoded audio in 'audios' array
                    import base64
                    audio_base64 = response_data['audios'][0]
                    audio_data = base64.b64decode(audio_base64)
                    logger.info(f"Decoded base64 audio from Sarvam API")
                else:
                    # Fallback to direct content
                    audio_data = response.content
            except:
                # Direct binary response (legacy format)
                audio_data = response.content
            
            # Validate audio data size
            if len(audio_data) < 1000:
                logger.error(f"Sarvam TTS returned invalid audio data (size: {len(audio_data)})")
                return None
                
            # Save audio file
            with open(output_path, 'wb') as f:
                f.write(audio_data)
                
            logger.info(f"Sarvam TTS audio saved to: {output_path} (size: {len(audio_data)} bytes)")
            
            # Return the publicly accessible URL
            audio_url = urljoin(SERVER_URL, f'/audio/{filename}')
            logger.info(f"Sarvam TTS audio URL: {audio_url}")
            return audio_url
        else:
            logger.error(f"Sarvam TTS API error: {response.status_code} - {response.text}")
            return None
            
    except requests.exceptions.Timeout:
        logger.error("Sarvam TTS API request timed out.")
        return None
    except Exception as e:
        logger.error(f"Sarvam TTS generation error: {e}")
        return None

def make_outbound_call(phone_number, system_message=None, greeting=None):
    """Initiate an outbound call"""
    if not twilio_client:
        return {"success": False, "error": "Twilio client not initialized"}
    
    if not SERVER_URL:
        return {"success": False, "error": "SERVER_URL not configured"}
    
    # Default values
    if not system_message:
        system_message = "You are a helpful AI assistant. Give very short, direct answers (1-2 sentences max). Respond in the same language the user speaks. Be concise and helpful."
    
    if not greeting:
        greeting = "नमस्ते! मैं आपका AI असिस्टेंट हूँ। मैं आपकी कैसे सहायता कर सकता हूँ?"
    
    try:
        webhook_url = urljoin(SERVER_URL, '/voice_webhook')
        if not TWILIO_PHONE_NUMBER:
            return {"success": False, "error": "Twilio phone number not configured"}
            
        call = twilio_client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url,
            method='POST'
        )
        
        # Store conversation state
        conversations[call.sid] = {
            'history': [],
            'system_message': system_message,
            'greeting': greeting
        }
        
        logger.info(f"Initiated call {call.sid} to {phone_number}")
        return {"success": True, "call_sid": call.sid}
        
    except Exception as e:
        logger.error(f"Failed to initiate call to {phone_number}: {e}")
        return {"success": False, "error": str(e)}

# Web Routes
@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    """Dashboard with call management"""
    return render_template('dashboard.html', conversations=conversations)

@app.route('/make_call', methods=['POST'])
def handle_make_call():
    """Handle outbound call request from web interface"""
    phone_number = request.form.get('phone_number')
    system_message = request.form.get('system_message', '')
    greeting = request.form.get('greeting', '')
    
    if not phone_number:
        flash('Phone number is required', 'error')
        return redirect(url_for('index'))
    
    result = make_outbound_call(phone_number, system_message, greeting)
    
    if result['success']:
        flash(f'Call initiated successfully. Call SID: {result["call_sid"]}', 'success')
    else:
        flash(f'Failed to initiate call: {result["error"]}', 'error')
    
    return redirect(url_for('dashboard'))

# Twilio Webhook Routes
@app.route('/voice_webhook', methods=['POST'])
def voice_webhook():
    """Handle incoming call webhook from Twilio"""
    response = VoiceResponse()
    call_sid = request.form.get('CallSid')
    
    logger.info(f"Incoming call webhook for {call_sid}")
    
    # Retrieve or set up conversation state
    if call_sid not in conversations:
        # For inbound calls, define default greeting/system message
        conversations[call_sid] = {
            'history': [],
            'system_message': "You are a helpful AI assistant. Give very short, direct answers (1-2 sentences max). Respond in the same language the user speaks. Be concise and helpful.",
            'greeting': "नमस्ते! मैं आपका AI असिस्टेंट हूँ। मैं आपकी कैसे सहायता कर सकता हूँ?"
        }
    
    call_data = conversations[call_sid]
    greeting = call_data['greeting']
    greeting_language = detect_language(greeting)
    
    # Generate and play greeting using Sarvam TTS
    greeting_audio_url = generate_sarvam_tts(greeting, language=greeting_language, voice="anushka")
    
    if greeting_audio_url:
        logger.info(f"Playing Sarvam TTS greeting: {greeting_audio_url}")
        # Minimal pause for faster response
        response.pause(length=0.5)
        response.play(greeting_audio_url)
    else:
        # Only fallback if Sarvam completely fails
        lang_code = 'hi-IN' if greeting_language == 'hi' else 'en-US'
        response.say("Hello, I am your AI assistant. How can I help you?", language='en-US')
    
    # Set up speech gathering with enhanced recognition settings
    gather = Gather(
        input='speech',
        timeout=15,  # Faster timeout for better conversation flow
        action='/process_speech',
        method='POST',
        speech_timeout=2,  # Quicker silence detection
        finish_on_key='#',  # Allow user to press # to finish
        language='hi-IN,en-US,te-IN',  # Multi-language support
        enhanced=True,  # Enhanced speech recognition
        speech_model='phone_call'  # Optimized for phone audio quality
    )
    
    response.append(gather)
    
    # If no speech detected after timeout, end call politely
    response.say("मुझे कोई आवाज़ नहीं सुनाई दी। धन्यवाद बात करने के लिए। अच्छा दिन हो।", language='hi-IN')
    response.hangup()
    
    return str(response)

@app.route('/process_speech', methods=['POST'])
def process_speech():
    """Process speech input and generate AI response"""
    response = VoiceResponse()
    call_sid = request.form.get('CallSid')
    speech_result = request.form.get('SpeechResult', '').strip()
    
    logger.info(f"Processing speech for {call_sid}: {speech_result}")
    
    if call_sid not in conversations:
        logger.error(f"No conversation found for {call_sid}")
        response.say("Sorry, there was an error. Please call again.", language='en-US')
        response.hangup()
        return str(response)
    
    call_data = conversations[call_sid]
    
    if not speech_result:
        response.say("मुझे आपकी बात समझ नहीं आई। कृपया दोबारा कहें।", language='hi-IN')
        # Continue gathering speech with optimized settings
        gather = Gather(
            input='speech',
            timeout=15,
            action='/process_speech',
            method='POST',
            speech_timeout=2,
            finish_on_key='#',
            language='hi-IN,en-US,te-IN',
            enhanced=True,
            speech_model='phone_call'
        )
        response.append(gather)
        response.say("समय समाप्त हो गया। धन्यवाद बात करने के लिए।", language='hi-IN')
        response.hangup()
        return str(response)
    
    # Add user input to conversation history
    call_data['history'].append({"role": "user", "content": speech_result})
    
    # Get AI response
    ai_response = get_llm_response(
        speech_result,
        call_data['history'],
        call_data['system_message']
    )
    
    # Add AI response to conversation history
    call_data['history'].append({"role": "assistant", "content": ai_response})
    
    # Generate AI response using Sarvam TTS
    response_language = detect_language(ai_response)
    audio_url = generate_sarvam_tts(ai_response, language=response_language, voice="anushka")
    
    if audio_url:
        logger.info(f"Playing Sarvam TTS response: {audio_url}")
        response.pause(length=0.3)
        response.play(audio_url)
    else:
        # Minimal fallback only if Sarvam fails
        response.say("I apologize, there was a technical issue. Please try again.", language='en-US')
    
    # Continue conversation with enhanced recognition
    gather = Gather(
        input='speech',
        timeout=15,  # Faster interaction flow
        action='/process_speech',
        method='POST',
        speech_timeout=2,  # Minimal silence for quick responses
        finish_on_key='#',
        language='hi-IN,en-US,te-IN',
        enhanced=True,
        speech_model='phone_call'
    )
    response.append(gather)
    
    # End conversation if no response after timeout
    response.say("समय समाप्त हो गया। धन्यवाद बात करने के लिए। अच्छा दिन हो।", language='hi-IN')
    response.hangup()
    
    return str(response)

@app.route('/audio/<filename>')
def serve_audio(filename):
    """Serve generated audio files with proper headers for Twilio"""
    try:
        file_path = os.path.join(AUDIO_DIR, filename)
        if os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            logger.info(f"Serving audio file: {filename} (size: {file_size} bytes)")
            
            # Determine correct MIME type
            if filename.endswith('.mp3'):
                content_type = 'audio/mpeg'
            elif filename.endswith('.wav'):
                content_type = 'audio/wav'
            else:
                content_type = 'audio/wav'  # Default to WAV
            
            # Use send_file for proper streaming with Twilio-compatible headers
            def generate():
                with open(file_path, 'rb') as f:
                    while True:
                        data = f.read(4096)  # Read in chunks
                        if not data:
                            break
                        yield data
            
            from flask import Response
            response = Response(generate(), content_type=content_type)
            response.headers['Content-Length'] = str(file_size)
            response.headers['Accept-Ranges'] = 'bytes'
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            
            logger.info(f"Successfully serving audio file: {filename}")
            return response
        else:
            logger.error(f"Audio file not found: {filename}")
            return "File not found", 404
    except Exception as e:
        logger.error(f"Error serving audio {filename}: {e}")
        return "Error serving file", 500

@app.route('/api/status')
def api_status():
    """API status endpoint"""
    status = {
        "twilio_configured": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "sarvam_configured": bool(SARVAM_API_KEY),
        "server_url_configured": bool(SERVER_URL),
        "active_conversations": len(conversations)
    }
    return jsonify(status)

@app.route('/test_tts/<text>')
def test_tts(text):
    """Test TTS generation for debugging"""
    try:
        audio_url = generate_sarvam_tts(text, "en", "anushka")
        if audio_url:
            return jsonify({"success": True, "audio_url": audio_url})
        else:
            return jsonify({"success": False, "error": "TTS generation failed"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == '__main__':
    # Validate configuration
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        logger.error("ERROR: Missing Twilio credentials in environment variables!")
    
    if not OPENROUTER_API_KEY:
        logger.error("ERROR: Missing OpenRouter API key in environment variables!")
    
    if not SARVAM_API_KEY:
        logger.error("ERROR: Missing Sarvam AI API key in environment variables!")
    
    if not SERVER_URL:
        logger.warning("WARNING: SERVER_URL not set. Webhooks will not work properly!")
    
    logger.info("Starting Voice AI Agent application...")
    app.run(debug=True, host='0.0.0.0', port=5000)
