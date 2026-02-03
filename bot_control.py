"""
Bot control manager for enabling/disabling bot and managing human takeover mode.
"""
import logging
import threading
from typing import Optional, Dict
from datetime import datetime
import json
import os

import config
from state_store import GoogleSheetsKVStore, should_use_google_state

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
        default_enabled = config.BOT_ENABLED if hasattr(config, "BOT_ENABLED") else True
        
        # Global bot enabled flag (default from config)
        self.bot_enabled = default_enabled
        
        # Per-customer human mode (phone_number -> True if human mode, False if bot mode)
        self.human_mode: Dict[str, bool] = {}

        # Google Sheets persistence (preferred on Render/free)
        self._use_google_state = should_use_google_state()
        self._store: Optional[GoogleSheetsKVStore] = None
        self._state_key = "global"
        self._last_reload = 0.0
        self._reload_ttl_seconds = 10  # keep fresh with low overhead

        if self._use_google_state:
            try:
                self._store = GoogleSheetsKVStore(
                    sheet_id=config.GOOGLE_SHEET_ID,
                    credentials_path=config.GOOGLE_SHEETS_CREDENTIALS_PATH,
                    worksheet_name=config.BOT_CONTROL_SHEET,
                )
            except Exception as e:
                logger.warning(f"⚠️ Failed to init Google Sheets bot-control store: {e}. Falling back to local JSON.")
                self._use_google_state = False
                self._store = None

        # Load saved state (Sheets first, then file)
        self._load_state()
    
    def _load_state(self):
        """Load bot control state from file."""
        # Prefer Google Sheets state if enabled
        if self._use_google_state and self._store:
            try:
                raw = self._store.get_json(self._state_key)
                if raw:
                    data = json.loads(raw)
                    self.bot_enabled = bool(data.get("bot_enabled", True))
                    self.human_mode = data.get("human_mode", {}) or {}
                    logger.info(
                        f"✅ Loaded bot control state from Sheets: bot_enabled={self.bot_enabled}, "
                        f"human_mode_count={len(self.human_mode)}"
                    )
                    self._last_reload = datetime.now().timestamp()
                    return
            except Exception as e:
                logger.warning(f"⚠️ Error loading bot control state from Sheets: {e}")

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
        data = {
            "bot_enabled": self.bot_enabled,
            "human_mode": self.human_mode,
            "last_updated": datetime.now().isoformat(),
        }

        # Persist to Sheets first (durable on Render)
        if self._use_google_state and self._store:
            try:
                self._store.set_json(self._state_key, json.dumps(data))
            except Exception as e:
                logger.warning(f"⚠️ Error saving bot control state to Sheets: {e}")

        # Also persist to local file (useful for local dev)
        try:
            with open(self.storage_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving bot control state: {e}")

    def _maybe_reload_state(self):
        """Reload state from Sheets occasionally to stay consistent after restarts or external changes."""
        if not (self._use_google_state and self._store):
            return
        now = datetime.now().timestamp()
        if (now - self._last_reload) < self._reload_ttl_seconds:
            return
        try:
            raw = self._store.get_json(self._state_key)
            if raw:
                data = json.loads(raw)
                self.bot_enabled = bool(data.get("bot_enabled", True))
                self.human_mode = data.get("human_mode", {}) or {}
            self._last_reload = now
        except Exception:
            # Keep existing state on errors
            self._last_reload = now
    
    def is_bot_enabled(self) -> bool:
        """Check if bot is globally enabled."""
        with self.lock:
            self._maybe_reload_state()
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
            self._maybe_reload_state()
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
            self._maybe_reload_state()
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
