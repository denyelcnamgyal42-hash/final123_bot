"""
Google Sheets-backed Key-Value store for persistent state.
Provides durable storage for sessions and bot control state on Render free tier.
"""
import logging
import os
import threading
import time
import json
from typing import Dict, Any, Optional

import config

logger = logging.getLogger(__name__)

# Try to import gspread
try:
    import gspread
    from gspread.exceptions import WorksheetNotFound
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    gspread = None
    WorksheetNotFound = None
    Credentials = None
    logger.warning("gspread not available. Google Sheets state store will not work.")


def should_use_google_state() -> bool:
    """Check if Google Sheets state storage should be used."""
    return (GSPREAD_AVAILABLE and 
            config.GOOGLE_SHEET_ID and 
            config.GOOGLE_SHEETS_CREDENTIALS_PATH)


class GoogleSheetsKVStore:
    """
    A Google Sheets-backed Key-Value store for persistent state.
    Supports caching and batched writes to minimize API calls.
    """
    def __init__(self, sheet_id: str, credentials_path: str, worksheet_name: str):
        """
        Initialize Google Sheets KV store.
        
        Args:
            sheet_id: Google Sheet ID
            credentials_path: Path to Google credentials JSON file
            worksheet_name: Name of the worksheet to use for storage
        """
        self.sheet_id = sheet_id
        self.credentials_path = credentials_path
        self.worksheet_name = worksheet_name
        self._client = None
        self._spreadsheet = None
        self._worksheet = None
        self._initialized = False
        self._last_connection_attempt = 0
        self._connection_cooldown = 60  # 60 seconds cooldown
        self.lock = threading.RLock()
        
        self._cache: Dict[str, Dict[str, Any]] = {}  # {key: {value: ..., row_num: ..., dirty: bool}}
        self._key_to_row: Dict[str, int] = {}  # {key: row_num}
        self._last_flush_time = 0
        self._flush_interval = 15  # seconds
        self._max_dirty_items = 5  # Flush if more than this many items are dirty
        
        self._init_sheets()
        self._start_flush_scheduler()
    
    def _get_service_account_email(self) -> str:
        """Get service account email from credentials file."""
        try:
            with open(self.credentials_path, 'r') as f:
                creds_data = json.load(f)
                return creds_data.get('client_email', 'Not found')
        except Exception:
            return 'Not found'
    
    def _init_sheets(self, force_reconnect: bool = False):
        """Initialize Google Sheets connection and load data."""
        current_time = time.time()
        if (not force_reconnect and self._initialized and
            (current_time - self._last_connection_attempt) < self._connection_cooldown):
            return
        
        self._last_connection_attempt = current_time
        try:
            if not self.sheet_id:
                raise ValueError("GOOGLE_SHEET_ID is not set")
            if not self.credentials_path or not os.path.exists(self.credentials_path):
                raise FileNotFoundError(f"Google Sheets credentials file not found: {self.credentials_path}")
            
            scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file"]
            creds = Credentials.from_service_account_file(self.credentials_path, scopes=scope)
            self._client = gspread.authorize(creds)
            self._spreadsheet = self._client.open_by_key(self.sheet_id)
            
            try:
                self._worksheet = self._spreadsheet.worksheet(self.worksheet_name)
                logger.info(f"✅ Found existing state sheet: {self.worksheet_name}")
            except WorksheetNotFound:
                logger.info(f"Creating new state sheet: {self.worksheet_name}")
                self._worksheet = self._spreadsheet.add_worksheet(title=self.worksheet_name, rows=1000, cols=2)
                self._worksheet.append_row(["Key", "Value"])
                self._worksheet.format('A1:B1', {'textFormat': {'bold': True}})
                logger.info(f"✅ Created new state sheet: {self.worksheet_name}")
            
            self._load_all_data_to_cache()
            self._initialized = True
            logger.info(f"✅ Google Sheet State Store '{self.worksheet_name}' initialized.")
        except gspread.exceptions.SpreadsheetNotFound:
            service_email = self._get_service_account_email()
            error_msg = (f"❌ Google Sheet with ID '{self.sheet_id}' not found for state store '{self.worksheet_name}'.\n"
                        f"Please check:\n1. Sheet ID is correct in your .env file\n"
                        f"2. Sheet is shared with service account email: {service_email}\n"
                        f"3. Service account has 'Editor' permissions")
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Failed to initialize Google Sheet State Store '{self.worksheet_name}': {e}", exc_info=True)
            raise RuntimeError(f"Failed to initialize state store: {e}")
    
    def _load_all_data_to_cache(self):
        """Loads all data from the worksheet into the cache."""
        with self.lock:
            self._cache = {}
            self._key_to_row = {}
            if not self._worksheet:
                return
            
            all_values = self._worksheet.get_all_values()
            if len(all_values) <= 1:  # Only header or empty
                return
            
            for i, row in enumerate(all_values[1:], start=2):  # Start from row 2
                if len(row) >= 2 and row[0]:
                    key = row[0]
                    try:
                        value = json.loads(row[1])  # Value is stored as JSON string
                    except json.JSONDecodeError:
                        value = row[1]  # Store as raw string if not JSON
                    self._cache[key] = {"value": value, "row_num": i, "dirty": False}
                    self._key_to_row[key] = i
            logger.debug(f"Loaded {len(self._cache)} items into cache for '{self.worksheet_name}'.")
    
    def _start_flush_scheduler(self):
        """Starts a background thread to periodically flush dirty cache items."""
        def flush_job():
            while True:
                time.sleep(self._flush_interval)
                self.flush_dirty_items()
        thread = threading.Thread(target=flush_job, daemon=True)
        thread.start()
    
    def get(self, key: str) -> Optional[Any]:
        """Retrieves a value by key."""
        with self.lock:
            self._init_sheets()  # Ensure connection
            item = self._cache.get(key)
            if item:
                return item["value"]
            return None
    
    def get_json(self, key: str) -> Optional[str]:
        """Retrieves a JSON string value by key."""
        value = self.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value)
    
    def set(self, key: str, value: Any):
        """Sets a value by key, marking it as dirty for later flush."""
        with self.lock:
            self._init_sheets()  # Ensure connection
            self._cache[key] = {"value": value, "row_num": self._key_to_row.get(key), "dirty": True}
            self._check_and_flush()
    
    def set_json(self, key: str, json_str: str):
        """Sets a JSON string value by key."""
        try:
            value = json.loads(json_str)
        except json.JSONDecodeError:
            value = json_str
        self.set(key, value)
    
    def delete(self, key: str):
        """Deletes a key-value pair."""
        with self.lock:
            self._init_sheets()  # Ensure connection
            if key in self._cache:
                row_num = self._cache[key]["row_num"]
                if row_num:
                    try:
                        self._worksheet.delete_rows(row_num)
                        logger.info(f"Deleted row {row_num} for key '{key}' in '{self.worksheet_name}'.")
                    except Exception as e:
                        logger.error(f"Error deleting row for key '{key}': {e}", exc_info=True)
                del self._cache[key]
                if key in self._key_to_row:
                    del self._key_to_row[key]
                self._rebuild_key_to_row_map()  # Rebuild map after deletion
    
    def get_all(self) -> Dict[str, Any]:
        """Retrieves all key-value pairs."""
        with self.lock:
            self._init_sheets()  # Ensure connection
            self.flush_dirty_items()  # Ensure cache is up-to-date with sheet
            self._load_all_data_to_cache()  # Reload to get external changes
            return {k: v["value"] for k, v in self._cache.items()}
    
    def _check_and_flush(self):
        """Checks if a flush is needed based on interval or dirty item count."""
        current_time = time.time()
        dirty_count = sum(1 for item in self._cache.values() if item["dirty"])
        if (current_time - self._last_flush_time > self._flush_interval or
            dirty_count >= self._max_dirty_items):
            self.flush_dirty_items()
    
    def flush_dirty_items(self):
        """Flushes all dirty items from the cache to the Google Sheet."""
        with self.lock:
            if not self._worksheet:
                logger.warning(f"Cannot flush dirty items: worksheet for '{self.worksheet_name}' not initialized.")
                return
            
            dirty_updates: list = []
            dirty_inserts: list = []
            keys_to_update_row_num = []
            
            for key, item in self._cache.items():
                if item["dirty"]:
                    value_str = json.dumps(item["value"])  # Store value as JSON string
                    if item["row_num"]:  # Existing row
                        dirty_updates.append({
                            'range': f'A{item["row_num"]}:B{item["row_num"]}',
                            'values': [[key, value_str]]
                        })
                    else:  # New row
                        dirty_inserts.append([key, value_str])
                        keys_to_update_row_num.append(key)
                    item["dirty"] = False  # Mark as clean
            
            if not dirty_updates and not dirty_inserts:
                self._last_flush_time = time.time()
                return
            
            try:
                if dirty_updates:
                    self._worksheet.batch_update(dirty_updates)
                    logger.debug(f"Flushed {len(dirty_updates)} updates to '{self.worksheet_name}'.")
                
                if dirty_inserts:
                    # Append new rows
                    self._worksheet.append_rows(dirty_inserts)
                    logger.debug(f"Flushed {len(dirty_inserts)} inserts to '{self.worksheet_name}'.")
                    # After appending, we need to update row numbers for newly inserted keys
                    self._rebuild_key_to_row_map()
                
                self._last_flush_time = time.time()
                logger.info(f"✅ State for '{self.worksheet_name}' flushed successfully.")
            except Exception as e:
                logger.error(f"❌ Error flushing dirty items to '{self.worksheet_name}': {e}", exc_info=True)
                # On error, mark items as dirty again so they are retried
                for key, item in self._cache.items():
                    if not item["row_num"] and key in keys_to_update_row_num:  # Only new inserts
                        item["dirty"] = True
                    elif item["row_num"] and any(upd['range'].startswith(f'A{item["row_num"]}') for upd in dirty_updates):
                        item["dirty"] = True
    
    def _rebuild_key_to_row_map(self):
        """Rebuilds the key-to-row number map after inserts/deletes."""
        with self.lock:
            if not self._worksheet:
                return
            all_values = self._worksheet.get_all_values()
            self._key_to_row = {}
            for i, row in enumerate(all_values[1:], start=2):
                if len(row) >= 1 and row[0]:
                    self._key_to_row[row[0]] = i
                    # Update cache with correct row_num if it was a new insert
                    if row[0] in self._cache and not self._cache[row[0]]["row_num"]:
                        self._cache[row[0]]["row_num"] = i
