"""
Bot control manager for enabling/disabling bot and managing human takeover mode.
"""
import logging
import threading
from typing import Optional, Dict
from datetime import datetime
import json
import os

logger = logging.getLogger(__name__)

class BotControlManager:
    """Manages bot enabled/disabled state and per-customer human mode."""
    
    def __init__(self, storage_file: str = "bot_control.json"):
        """
        Initialize bot control manager.
        
        Args:
            storage_file: Path to JSON file for persisting bot control state
        """
        self.storage_file = storage_file
        self.lock = threading.RLock()
        
        # Load config default for bot enabled
        try:
            import config
            default_enabled = config.BOT_ENABLED
        except:
            default_enabled = True
        
        # Global bot enabled flag (default from config)
        self.bot_enabled = default_enabled
        
        # Per-customer human mode (phone_number -> True if human mode, False if bot mode)
        self.human_mode: Dict[str, bool] = {}
        
        # Load saved state (will override default if file exists)
        self._load_state()
    
    def _load_state(self):
        """Load bot control state from file."""
        if not os.path.exists(self.storage_file):
            logger.info("Bot control state file not found. Using defaults (bot enabled).")
            return
        
        try:
            with open(self.storage_file, 'r') as f:
                data = json.load(f)
                self.bot_enabled = data.get("bot_enabled", True)
                self.human_mode = data.get("human_mode", {})
                logger.info(f"✅ Loaded bot control state: bot_enabled={self.bot_enabled}, human_mode_count={len(self.human_mode)}")
        except Exception as e:
            logger.error(f"Error loading bot control state: {e}")
            # Use defaults on error
            self.bot_enabled = True
            self.human_mode = {}
    
    def _save_state(self):
        """Save bot control state to file."""
        try:
            with open(self.storage_file, 'w') as f:
                json.dump({
                    "bot_enabled": self.bot_enabled,
                    "human_mode": self.human_mode,
                    "last_updated": datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving bot control state: {e}")
    
    def is_bot_enabled(self) -> bool:
        """Check if bot is globally enabled."""
        with self.lock:
            return self.bot_enabled
    
    def set_bot_enabled(self, enabled: bool) -> bool:
        """
        Set global bot enabled/disabled state.
        
        Args:
            enabled: True to enable bot, False to disable
            
        Returns:
            True if successful
        """
        with self.lock:
            self.bot_enabled = enabled
            self._save_state()
            logger.info(f"✅ Bot globally {'enabled' if enabled else 'disabled'}")
            return True
    
    def is_human_mode(self, phone_number: str) -> bool:
        """
        Check if a specific customer is in human mode.
        
        Args:
            phone_number: Customer phone number
            
        Returns:
            True if customer is in human mode, False if bot mode
        """
        with self.lock:
            return self.human_mode.get(phone_number, False)
    
    def set_human_mode(self, phone_number: str, human_mode: bool) -> bool:
        """
        Set human mode for a specific customer.
        
        Args:
            phone_number: Customer phone number
            human_mode: True to enable human mode, False to enable bot mode
            
        Returns:
            True if successful
        """
        with self.lock:
            if human_mode:
                self.human_mode[phone_number] = True
                logger.info(f"✅ Enabled human mode for {phone_number}")
            else:
                if phone_number in self.human_mode:
                    del self.human_mode[phone_number]
                logger.info(f"✅ Disabled human mode for {phone_number} (back to bot mode)")
            self._save_state()
            return True
    
    def should_use_bot(self, phone_number: str) -> bool:
        """
        Determine if bot should process message for a customer.
        
        Args:
            phone_number: Customer phone number
            
        Returns:
            True if bot should process, False if human should handle
        """
        with self.lock:
            # If bot is globally disabled, don't use bot
            if not self.bot_enabled:
                return False
            
            # If customer is in human mode, don't use bot
            if self.is_human_mode(phone_number):
                return False
            
            # Otherwise, use bot
            return True
    
    def get_all_human_mode_customers(self) -> Dict[str, bool]:
        """Get all customers currently in human mode."""
        with self.lock:
            return self.human_mode.copy()
    
    def clear_human_mode(self, phone_number: str) -> bool:
        """
        Clear human mode for a customer (return to bot mode).
        
        Args:
            phone_number: Customer phone number
            
        Returns:
            True if successful
        """
        return self.set_human_mode(phone_number, False)

# Global instance
_bot_control_manager = None

def get_bot_control_manager() -> BotControlManager:
    """Get the global bot control manager instance."""
    global _bot_control_manager
    if _bot_control_manager is None:
        _bot_control_manager = BotControlManager()
    return _bot_control_manager
