"""
Main entry point for the hotel booking chatbot.
Starts WhatsApp webhook server with dashboard routes registered.
"""
import logging
import signal
import sys
from whatsapp_webhook import app as webhook_app
from employee_dashboard import app as dashboard_app
from langchain_agent import initialize_agent
import config

# Configure logging for production
log_level = logging.INFO if not config.IS_PRODUCTION else logging.WARNING
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


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
    
    # Register dashboard routes with webhook app (for Render - single port deployment)
    # This makes dashboard accessible via the main service URL
    try:
        # Register all dashboard routes with the webhook app
        for rule in dashboard_app.url_map.iter_rules():
            if rule.endpoint != 'static':  # Skip static files
                # Get the view function
                view_func = dashboard_app.view_functions[rule.endpoint]
                # Register with webhook app
                webhook_app.add_url_rule(
                    rule.rule,
                    endpoint=f"dashboard_{rule.endpoint}",  # Prefix to avoid conflicts
                    view_func=view_func,
                    methods=rule.methods
                )
        logger.info("✅ Dashboard routes registered with webhook app (accessible on same port)")
    except Exception as e:
        logger.warning(f"⚠️ Could not register dashboard routes: {e}")
        logger.warning("Dashboard will only be accessible on separate port (not available on Render)")
    
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # For Render: Only run webhook app (dashboard routes are registered on it)
    # Run in main thread so Render detects the process is alive
    logger.info("=" * 60)
    logger.info("🎉 Hotel Booking Chatbot is running!")
    logger.info("=" * 60)
    logger.info(f"📱 WhatsApp Webhook: http://0.0.0.0:{config.PORT}/webhook")
    logger.info(f"🏨 Employee Dashboard: http://0.0.0.0:{config.PORT}/dashboard")
    logger.info("=" * 60)
    logger.info("Starting server on main thread (Render-compatible)...")
    logger.info("=" * 60)
    
    # Run webhook app in main thread (this blocks, keeping process alive)
    # Dashboard routes are already registered on webhook_app
    try:
        webhook_app.run(
            host="0.0.0.0",
            port=config.PORT,
            debug=False,
            threaded=True,
            use_reloader=False
        )
    except KeyboardInterrupt:
        signal_handler(None, None)
    except Exception as e:
        logger.error(f"❌ Error running server: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
