"""
Main entry point for the hotel booking chatbot.
Starts both WhatsApp webhook and employee dashboard servers.
"""
import logging
import threading
import signal
import sys
import time
from whatsapp_webhook import app as webhook_app
from employee_dashboard import app as dashboard_app
from langchain_agent import initialize_agent
import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def run_webhook():
    """Run the WhatsApp webhook server."""
    try:
        logger.info(f"🚀 Starting WhatsApp webhook server on port {config.PORT}")
        webhook_app.run(
            host="0.0.0.0",
            port=config.PORT,
            debug=config.DEBUG,
            threaded=True,
            use_reloader=False  # Disable reloader when running in thread
        )
    except Exception as e:
        logger.error(f"❌ Error starting webhook server: {e}", exc_info=True)


def run_dashboard():
    """Run the employee dashboard server."""
    try:
        logger.info(f"🚀 Starting employee dashboard on port {config.DASHBOARD_PORT}")
        dashboard_app.run(
            host="0.0.0.0",
            port=config.DASHBOARD_PORT,
            debug=config.DEBUG,
            threaded=True,
            use_reloader=False  # Disable reloader when running in thread
        )
    except Exception as e:
        logger.error(f"❌ Error starting dashboard server: {e}", exc_info=True)


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    logger.info("\n🛑 Shutting down servers...")
    sys.exit(0)


def main():
    """Main function to start both servers."""
    # Initialize the LangChain agent
    logger.info("🔧 Initializing LangChain agent...")
    if not initialize_agent():
        logger.error("❌ Failed to initialize agent. Exiting.")
        sys.exit(1)
    logger.info("✅ Agent initialized successfully")
    
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start webhook server in a separate thread
    webhook_thread = threading.Thread(target=run_webhook, daemon=True)
    webhook_thread.start()
    logger.info(f"✅ Webhook server thread started")
    
    # Give webhook a moment to start
    time.sleep(1)
    
    # Start dashboard server in a separate thread
    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()
    logger.info(f"✅ Dashboard server thread started")
    
    # Give dashboard a moment to start
    time.sleep(1)
    
    logger.info("=" * 60)
    logger.info("🎉 Hotel Booking Chatbot is running!")
    logger.info("=" * 60)
    logger.info(f"📱 WhatsApp Webhook: http://0.0.0.0:{config.PORT}")
    logger.info(f"🏨 Employee Dashboard: http://0.0.0.0:{config.DASHBOARD_PORT}/dashboard")
    logger.info("=" * 60)
    logger.info("Press Ctrl+C to stop both servers")
    logger.info("=" * 60)
    
    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
            # Check if threads are still alive
            if not webhook_thread.is_alive():
                logger.error("❌ Webhook server thread died!")
            if not dashboard_thread.is_alive():
                logger.error("❌ Dashboard server thread died!")
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    main()
