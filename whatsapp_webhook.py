from flask import Flask, request, jsonify
import requests 
import config 
import logging 
import re 
import time
from datetime import datetime
from threading import Thread 
from queue import Queue 
from functools import wraps 
# Import agent module - agent will be initialized in main.py
try:
    import langchain_agent
except ImportError:
    langchain_agent = None

from flask_limiter import Limiter 
from flask_limiter.util import get_remote_address

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if langchain_agent is None:
    logger.warning("WhatsApp agent module not available")

app = Flask(__name__)

limiter = Limiter(
    app=app, 
    key_func=get_remote_address,
    default_limits=["2000 per hour", "80 per second"],
    storage_uri="memory://",
    strategy="fixed-window",
    headers_enabled=True
)
message_queue = Queue(maxsize=1000)

# Request logging (only for webhook endpoint to reduce noise)
@app.before_request
def log_request_info():
    """Log webhook requests for monitoring."""
    if request.path == "/webhook":
        logger.info(f"📥 Incoming {request.method} request to {request.path}")

def validate_phone_number(phone_number: str) -> bool:
    """Validate WhatsApp phone number format."""
    cleaned = re.sub(r'\D', '', phone_number)
    return len(cleaned) >= 10 and len(cleaned) <= 15

def sanitize_message(text: str) -> str:
    """Sanitize message text."""
    sanitized = re.sub(r'[\x00-\x1F\x7F]', '', text)
    return sanitized[:4000]

def send_whatsapp_message(phone_number: str, message: str, message_id: str = None):
    """Send a WhatsApp message using Meta API."""
    if not validate_phone_number(phone_number):
        logger.error(f"Invalid phone number: {phone_number}")
        return None
    
    message = sanitize_message(message)
    
    url = config.WHATSAPP_API_URL
    
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone_number,
        "type": "text",
        "text": {"body": message}
    }
    
    if message_id:
        payload["context"] = {"message_id": message_id}
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(
                url, 
                json=payload, 
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                logger.info(f"✅ Message sent to {phone_number}")
                return response.json()
            elif response.status_code == 401:
                logger.error(f"❌ Authentication failed. Check WHATSAPP_ACCESS_TOKEN")
                logger.error(f"Response: {response.text}")
                return None  # Don't retry on auth errors
            elif response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 5))
                logger.warning(f"Rate limited. Retrying after {retry_after} seconds")
                time.sleep(retry_after)
                continue
            else:
                logger.error(f"❌ HTTP Error {response.status_code}: {response.text}")
                # Log full error for debugging
                try:
                    error_json = response.json()
                    logger.error(f"Error details: {error_json}")
                except:
                    pass
                
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout sending message (attempt {attempt + 1})")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {e}")
        
        time.sleep(2 ** attempt)
    
    logger.error(f"Failed to send message after {max_retries} attempts")
    return None

def process_message_async():
    """Background worker to process messages."""
    while True:
        try:
            data = message_queue.get()
            if data is None:
                break
                
            phone, text, name, message_id = data
            
            logger.info(f"Processing async message from {phone}")
            
            try:
                # Check bot control status
                from bot_control import get_bot_control_manager
                bot_control = get_bot_control_manager()
                
                # Check if bot should process this message
                if not bot_control.should_use_bot(phone):
                    # Bot is disabled globally or customer is in human mode
                    if not bot_control.is_bot_enabled():
                        human_msg = (
                            "Hello! Our team is currently replying to messages manually. "
                            "Please wait for a staff member to respond. Thank you for your patience! 😊"
                        )
                    else:
                        # Customer is in human mode
                        human_msg = (
                            "Hello! A staff member will respond to your message shortly. "
                            "Thank you for your patience! 😊"
                        )
                    
                    logger.info(f"🤖 Bot disabled for {phone}, sending human mode message")
                    send_whatsapp_message(phone, human_msg, message_id)
                    message_queue.task_done()
                    continue
                
                # Process with agent - access through module to get current value
                agent = langchain_agent.get_agent() if langchain_agent else None
                if agent is None:
                    error_msg = "Chatbot is initializing. Please try again in a moment."
                    logger.error("Agent not initialized when processing message")
                    send_whatsapp_message(phone, error_msg, message_id)
                    message_queue.task_done()
                    continue
                
                response_text = agent.process_message(text, phone, name)
                
                # Send response
                send_whatsapp_message(phone, response_text, message_id)
                
            except Exception as e:
                logger.error(f"❌ Error in async processing: {e}", exc_info=True)
                error_msg = "I apologize, but I encountered an error. Please try again."
                send_whatsapp_message(phone, error_msg, message_id)
                
            message_queue.task_done()
            
        except Exception as e:
            logger.error(f"❌ Error in async worker: {e}", exc_info=True)

# Start background worker
worker_thread = Thread(target=process_message_async, daemon=True)
worker_thread.start()

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verify webhook for Meta API."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    logger.info(f"🔍 Webhook verification attempt:")
    logger.info(f"   Mode: {mode}")
    logger.info(f"   Token received: {token}")
    logger.info(f"   Token expected: {config.WHATSAPP_VERIFY_TOKEN}")
    logger.info(f"   Challenge: {challenge}")
    logger.info(f"   Full URL: {request.url}")
    logger.info(f"   Headers: {dict(request.headers)}")
    
    if mode == "subscribe" and token == config.WHATSAPP_VERIFY_TOKEN:
        logger.info("✅ Webhook verified successfully")
        return challenge, 200
    else:
        logger.warning(f"❌ Webhook verification failed. Expected: {config.WHATSAPP_VERIFY_TOKEN}, Got: {token}")
        return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
@limiter.limit("80 per second")
def handle_webhook():
    """Handle incoming WhatsApp messages."""
    logger.info("=" * 60)
    logger.info("📥 Received POST to /webhook")
    logger.info(f"   Headers: {dict(request.headers)}")
    logger.info(f"   Content-Type: {request.content_type}")
    logger.info(f"   Content-Length: {request.content_length}")
    logger.info(f"   Remote Address: {request.remote_addr}")
    logger.info(f"   Full URL: {request.url}")
    
    try:
        # Get raw data first to see what we're receiving
        raw_data = request.get_data(as_text=True)
        logger.info(f"   Raw request data: {raw_data[:500]}")  # First 500 chars
        
        # Parse the JSON data
        data = request.get_json()
        logger.info(f"   Parsed JSON data: {data}")
        
        if not data:
            logger.warning("⚠️ Empty request received - no JSON data")
            logger.warning(f"   Raw data was: {raw_data[:200] if raw_data else 'None'}")
            return jsonify({"status": "ignored", "reason": "empty_request"}), 200
        
        logger.info(f"   Object type: {data.get('object')}")
        
        if data.get("object") != "whatsapp_business_account":
            logger.warning(f"⚠️ Invalid object type: {data.get('object')} (expected: whatsapp_business_account)")
            logger.warning(f"   Full data: {data}")
            return jsonify({"status": "ignored", "reason": "invalid_object"}), 200
        
        entries = data.get("entry", [])
        logger.info(f"   Number of entries: {len(entries)}")
        
        if not entries:
            logger.warning("⚠️ No entries in webhook data")
            return jsonify({"status": "ignored", "reason": "no_entries"}), 200
        
        for entry in entries:
            changes = entry.get("changes", [])
            logger.info(f"   Entry ID: {entry.get('id')}, Changes: {len(changes)}")
            
            for change in changes:
                value = change.get("value", {})
                field = change.get("field", "unknown")
                logger.info(f"   Change field: {field}")
                logger.info(f"   Value keys: {list(value.keys())}")
                
                # Handle messages
                if "messages" in value:
                    logger.info(f"   ✅ Found messages in webhook data!")
                    messages = value["messages"]
                    
                    for message in messages:
                        msg_type = message.get("type")
                        logger.info(f"   📨 Message type: {msg_type}")
                        logger.info(f"   📨 Message ID: {message.get('id')}")
                        
                        if msg_type == "text":
                            from_number = message.get("from", "")
                            message_text = message.get("text", {}).get("body", "")
                            message_id = message.get("id", "")
                            
                            # Get customer name
                            contacts = value.get("contacts", [{}])
                            customer_name = contacts[0].get("profile", {}).get("name", "Customer")
                            
                            logger.info(f"📩 Message from {from_number}: {message_text}")
                            
                            # Add to queue for async processing
                            try:
                                if not message_queue.full():
                                    message_queue.put((
                                        from_number,
                                        message_text,
                                        customer_name,
                                        message_id
                                    ))
                                    logger.info(f"📥 Queued message from {from_number}")
                                else:
                                    logger.error("Message queue is full!")
                                    send_whatsapp_message(from_number, "I'm busy. Please try again later.", message_id)
                            except Exception as e:
                                logger.error(f"❌ Error queuing message: {e}", exc_info=True)
                        else:
                            logger.info(f"   ℹ️ Skipping non-text message type: {msg_type}")
                else:
                    logger.info(f"   ℹ️ No 'messages' in value. Value contains: {list(value.keys())}")
                    # Log status updates or other webhook events
                    if "statuses" in value:
                        logger.info(f"   📊 Status update received (not a message)")
                    else:
                        logger.info(f"   ℹ️ Other webhook event (not a message)")
        
        logger.info("=" * 60)
        # Always return 200 immediately
        return jsonify({"status": "accepted"}), 200
    
    except Exception as e:
        logger.error(f"❌ Error handling webhook: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "Internal server error"}), 500

@app.route("/", methods=["GET"])
def root():
    """Root endpoint for health checks."""
    return jsonify({
        "status": "online",
        "service": "Hotel Booking WhatsApp Chatbot",
        "timestamp": datetime.now().isoformat()
    }), 200

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "queue_size": message_queue.qsize(),
        "agent_initialized": langchain_agent is not None and langchain_agent.get_agent() is not None
    }), 200


def cleanup():
    """Cleanup function for graceful shutdown."""
    logger.info("Shutting down...")
    message_queue.put(None)
    worker_thread.join(timeout=5)

import atexit
atexit.register(cleanup)

if __name__ == "__main__":
    logger.info(f"🚀 Starting WhatsApp webhook server on port {config.PORT}")
    app.run(
        host="0.0.0.0",
        port=config.PORT,
        debug=config.DEBUG,
        threaded=True
    )