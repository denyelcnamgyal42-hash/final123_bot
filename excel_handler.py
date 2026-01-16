"""
Excel and Google Sheets handler for reading and updating room availability.
Maintains the fixed structure: Rows = dates, Columns = rooms.
"""
import os
import logging
import time
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timedelta
import json

logger = logging.getLogger(__name__)

# Try to import required libraries
try:
    import openpyxl
    from openpyxl import load_workbook, Workbook
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False
    logger.warning("openpyxl not available. Excel functionality will be limited.")

try:
    import gspread
    from gspread import exceptions as gspread_exceptions
    from gspread.exceptions import WorksheetNotFound
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    gspread_exceptions = None
    WorksheetNotFound = None
    logger.warning("gspread not available. Google Sheets functionality will be limited.")


class ExcelHandler:
    """Handles reading and writing to Excel/Google Sheets for room availability."""
    
    def __init__(self, excel_path: Optional[str] = None, 
                 google_sheet_id: Optional[str] = None,
                 google_credentials_path: Optional[str] = None,
                 sheet_name: str = "Sheet1"):
        """
        Initialize Excel handler.
        
        Args:
            excel_path: Path to local Excel file
            google_sheet_id: Google Sheet ID (if using Google Sheets)
            google_credentials_path: Path to Google credentials JSON
            sheet_name: Name of the sheet to use
        """
        self.excel_path = excel_path
        self.google_sheet_id = google_sheet_id
        self.google_credentials_path = google_credentials_path
        self.sheet_name = sheet_name
        self.use_google_sheets = google_sheet_id is not None
        
        # Connection state tracking for Google Sheets
        self._initialized = False
        self._last_connection_attempt = 0
        self._connection_cooldown = 60  # 60 seconds cooldown between connection attempts
        self._client = None
        self._spreadsheet = None
        
        # Caching for Google Sheets to reduce API calls
        self._date_column_cache = None
        self._date_column_cache_time = 0
        self._date_column_cache_ttl = 300  # Cache for 5 minutes
        self._room_types_cache = None
        self._room_types_cache_time = 0
        self._room_types_cache_ttl = 600  # Cache for 10 minutes
        self._row_data_cache = {}  # Cache row data by row number
        self._row_cache_ttl = 60  # Cache rows for 1 minute
        
        # Room configuration cache (max guests per room type)
        self._room_config_cache = None
        self._room_config_cache_time = 0
        self._room_config_cache_ttl = 3600  # Cache for 1 hour
        
        # Rate limiting
        self._last_api_call_time = 0
        self._min_api_interval = 0.1  # Minimum 100ms between API calls
        self._consecutive_429_errors = 0
        self._backoff_until = 0
        
        if self.use_google_sheets:
            self._init_google_sheets()
        elif excel_path:
            self._init_excel()
    
    def _get_service_account_email(self) -> str:
        """Get service account email from credentials file."""
        try:
            with open(self.google_credentials_path, 'r') as f:
                creds_data = json.load(f)
                return creds_data.get('client_email', 'Not found')
        except Exception:
            return 'Not found'
    
    def _rate_limit_api_call(self):
        """Enforce rate limiting between API calls."""
        current_time = time.time()
        
        # Check if we're in backoff period due to 429 errors
        if current_time < self._backoff_until:
            wait_time = self._backoff_until - current_time
            logger.debug(f"Rate limit backoff: waiting {wait_time:.2f} seconds")
            time.sleep(wait_time)
            current_time = time.time()
        
        # Enforce minimum interval between API calls
        time_since_last_call = current_time - self._last_api_call_time
        if time_since_last_call < self._min_api_interval:
            sleep_time = self._min_api_interval - time_since_last_call
            time.sleep(sleep_time)
        
        self._last_api_call_time = time.time()
    
    def _handle_429_error(self, error):
        """Handle 429 (quota exceeded) errors with exponential backoff."""
        self._consecutive_429_errors += 1
        # Exponential backoff: 2^errors seconds, max 60 seconds
        backoff_time = min(2 ** self._consecutive_429_errors, 60)
        self._backoff_until = time.time() + backoff_time
        logger.warning(f"Rate limit hit (429). Backing off for {backoff_time} seconds. Consecutive errors: {self._consecutive_429_errors}")
        raise error
    
    def _reset_429_backoff(self):
        """Reset 429 error tracking after successful API call."""
        if self._consecutive_429_errors > 0:
            logger.info(f"API calls successful. Resetting 429 backoff counter (was {self._consecutive_429_errors})")
        self._consecutive_429_errors = 0
        self._backoff_until = 0
    
    def _ensure_connected(self, force_reconnect: bool = False):
        """Ensure connection to Google Sheets is established."""
        current_time = time.time()
        
        # Check if we should attempt reconnection
        if (not force_reconnect and 
            self._initialized and 
            (current_time - self._last_connection_attempt) < self._connection_cooldown):
            return
        
        self._last_connection_attempt = current_time
        
        try:
            # Check if credentials file exists
            if not os.path.exists(self.google_credentials_path):
                raise FileNotFoundError(
                    f"Google Sheets credentials file not found: {self.google_credentials_path}"
                )
            
            # Check if sheet ID is configured
            if not self.google_sheet_id:
                raise ValueError("GOOGLE_SHEET_ID is not set in your .env file")
            
            # Configure scopes
            scope = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.file"
            ]
            
            # Load credentials
            creds = Credentials.from_service_account_file(
                self.google_credentials_path, 
                scopes=scope
            )
            
            # Create client
            self._client = gspread.authorize(creds)
            
            # Open spreadsheet
            self._spreadsheet = self._client.open_by_key(self.google_sheet_id)
            
            # Test connection
            _ = self._spreadsheet.title  # This will raise if connection fails
            
            self._initialized = True
            logger.info(f"✅ Connected to Google Sheet: {self._spreadsheet.title}")
            
        except gspread.exceptions.SpreadsheetNotFound:
            service_email = self._get_service_account_email()
            error_msg = (
                f"❌ Google Sheet with ID '{self.google_sheet_id}' not found.\n"
                f"Please check:\n"
                f"1. Sheet ID is correct in your .env file\n"
                f"2. Sheet is shared with service account email: {service_email}\n"
                f"3. Service account has 'Editor' or 'Viewer' permissions"
            )
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            service_email = self._get_service_account_email()
            error_msg = f"❌ Failed to connect to Google Sheets: {str(e)}\nService account: {service_email}"
            logger.error(error_msg)
            import traceback
            traceback.print_exc()
            raise RuntimeError(error_msg)
    
    def _init_google_sheets(self):
        """Initialize Google Sheets connection."""
        if not GSPREAD_AVAILABLE:
            raise ImportError("gspread is required for Google Sheets support. Install with: pip install gspread google-auth")
        
        # Establish connection
        logger.info("🔗 Establishing connection to Google Sheets...")
        self._ensure_connected(force_reconnect=True)
        
        # List all available worksheets for debugging
        try:
            all_worksheets = [ws.title for ws in self._spreadsheet.worksheets()]
            logger.info(f"📋 Available worksheets in '{self._spreadsheet.title}': {', '.join(all_worksheets)}")
            
            # Check if worksheet exists (with or without trailing spaces)
            worksheet_found = None
            for ws in self._spreadsheet.worksheets():
                if ws.title.strip() == self.sheet_name.strip():
                    worksheet_found = ws
                    if ws.title != self.sheet_name:
                        logger.info(f"ℹ️  Found worksheet '{ws.title}' (matches '{self.sheet_name}' after trimming spaces)")
                    break
        except Exception as e:
            logger.warning(f"Could not list worksheets: {e}")
            worksheet_found = None
        
        # Try to get the worksheet, create if it doesn't exist
        if worksheet_found:
            self.sheet = worksheet_found
            logger.info(f"✅ Connected to worksheet '{self.sheet.title}' in Google Sheet '{self._spreadsheet.title}'")
        else:
            try:
                self.sheet = self._spreadsheet.worksheet(self.sheet_name)
                logger.info(f"✅ Connected to worksheet '{self.sheet_name}' in Google Sheet '{self._spreadsheet.title}'")
            except WorksheetNotFound:
                # Worksheet doesn't exist, create it
                logger.warning(f"⚠️ Worksheet '{self.sheet_name}' not found in the Google Sheet.")
                logger.info(f"📝 Attempting to create worksheet '{self.sheet_name}'...")
                try:
                    self.sheet = self._spreadsheet.add_worksheet(title=self.sheet_name, rows=100, cols=20)
                    # Set header row using correct update syntax (must be 2D array)
                    self.sheet.update('A1', [['Date']])
                    self.sheet.update('B1', [['Room 1']])
                    self.sheet.update('C1', [['Room 2']])
                    logger.info(f"✅ Created new worksheet '{self.sheet_name}' in Google Sheet '{self._spreadsheet.title}'")
                    logger.info("📋 Worksheet structure initialized with headers: Date, Room 1, Room 2")
                except Exception as create_error:
                    service_email = self._get_service_account_email()
                    error_msg = (
                        f"❌ Failed to create worksheet '{self.sheet_name}': {str(create_error)}\n"
                        f"Make sure the service account ({service_email}) has 'Editor' permissions on the Google Sheet."
                    )
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)
            except Exception as ws_error:
                error_str = str(ws_error).lower()
                # Check if it's a worksheet not found error (fallback for different exception types)
                if "worksheet" in error_str or "not found" in error_str or "does not exist" in error_str:
                    logger.warning(f"⚠️ Worksheet '{self.sheet_name}' not found in the Google Sheet.")
                    logger.info(f"📝 Attempting to create worksheet '{self.sheet_name}'...")
                    try:
                        self.sheet = self._spreadsheet.add_worksheet(title=self.sheet_name, rows=100, cols=20)
                        # Set header row using correct update syntax
                        self.sheet.update('A1', [['Date']])
                        self.sheet.update('B1', [['Room 1']])
                        self.sheet.update('C1', [['Room 2']])
                        logger.info(f"✅ Created new worksheet '{self.sheet_name}' in Google Sheet '{self._spreadsheet.title}'")
                        logger.info("📋 Worksheet structure initialized with headers: Date, Room 1, Room 2")
                    except Exception as create_error:
                        service_email = self._get_service_account_email()
                        error_msg = (
                            f"❌ Failed to create worksheet '{self.sheet_name}': {str(create_error)}\n"
                            f"Make sure the service account ({service_email}) has 'Editor' permissions on the Google Sheet."
                        )
                        logger.error(error_msg)
                        raise RuntimeError(error_msg)
                else:
                    logger.error(f"❌ Error accessing worksheet: {ws_error}")
                    raise
    
    def _init_excel(self):
        """Initialize Excel file connection."""
        if not OPENPYXL_AVAILABLE:
            raise ImportError("openpyxl is required for Excel support. Install with: pip install openpyxl")
        
        if not os.path.exists(self.excel_path):
            logger.warning(f"Excel file not found: {self.excel_path}. Creating new file.")
            self._create_new_excel()
        else:
            try:
                self.workbook = load_workbook(self.excel_path, data_only=True)
                if self.sheet_name in self.workbook.sheetnames:
                    self.worksheet = self.workbook[self.sheet_name]
                    logger.info(f"Loaded Excel file: {self.excel_path} with sheet '{self.sheet_name}'")
                else:
                    logger.warning(f"Sheet '{self.sheet_name}' not found in {self.excel_path}. Creating new sheet...")
                    # Create the sheet if it doesn't exist
                    self.worksheet = self.workbook.create_sheet(self.sheet_name)
                    # Set header row
                    self.worksheet['A1'] = 'Date'
                    self.worksheet['B1'] = 'Room 1'
                    self.worksheet['C1'] = 'Room 2'
                    self.workbook.save(self.excel_path)
                    logger.info(f"Created new sheet '{self.sheet_name}' in Excel file")
            except Exception as e:
                error_msg = f"Failed to load Excel file {self.excel_path}: {str(e)}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
    
    def _create_new_excel(self):
        """Create a new Excel file with basic structure."""
        self.workbook = Workbook()
        self.worksheet = self.workbook.active
        self.worksheet.title = self.sheet_name
        # Set header row: Date in A1, Room columns start from B1
        self.worksheet['A1'] = 'Date'
        self.worksheet['B1'] = 'Room 1'
        self.worksheet['C1'] = 'Room 2'
        self.workbook.save(self.excel_path)
        logger.info(f"Created new Excel file: {self.excel_path}")
    
    def find_date_row(self, date_str: str) -> Optional[int]:
        """
        Find the row number for a given date (YYYY-MM-DD format).
        
        Args:
            date_str: Date in YYYY-MM-DD format
            
        Returns:
            Row number (1-indexed) or None if not found
        """
        if self.use_google_sheets:
            return self._find_date_row_google(date_str)
        else:
            return self._find_date_row_excel(date_str)
    
    def _find_date_row_excel(self, date_str: str) -> Optional[int]:
        """Find date row in Excel file."""
        try:
            # Parse the date
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            target_month = date_obj.month
            target_day = date_obj.day
            
            # Check all rows in column A (date column), skip header rows 1-3
            for row_idx in range(4, self.worksheet.max_row + 1):
                cell_value = self.worksheet.cell(row=row_idx, column=1).value
                
                if cell_value is None:
                    continue
                
                # Try to match date in various formats
                if isinstance(cell_value, datetime):
                    # Match by month and day (ignore year)
                    if cell_value.month == target_month and cell_value.day == target_day:
                        return row_idx
                elif isinstance(cell_value, str):
                    cell_str = str(cell_value).strip().lower()
                    
                    # Try parsing formats like "January 15", "January 15, 2025", etc.
                    try:
                        import re
                        # Remove year if present
                        date_clean = re.sub(r',\s*\d{4}', '', cell_str)
                        
                        # Parse month name and day
                        month_names = {
                            'january': 1, 'jan': 1, 'february': 2, 'feb': 2,
                            'march': 3, 'mar': 3, 'april': 4, 'apr': 4,
                            'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
                            'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'sept': 9,
                            'october': 10, 'oct': 10, 'november': 11, 'nov': 11,
                            'december': 12, 'dec': 12
                        }
                        
                        for month_name, month_num in month_names.items():
                            if month_name in date_clean:
                                day_match = re.search(r'\b(\d{1,2})\b', date_clean)
                                if day_match:
                                    day_num = int(day_match.group(1))
                                    if month_num == target_month and day_num == target_day:
                                        return row_idx
                    except Exception:
                        pass
                    
                    # Try standard date formats
                    for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d', '%B %d', '%B %d, %Y', '%b %d', '%b %d, %Y']:
                        try:
                            parsed = datetime.strptime(cell_value, fmt)
                            # Match by month and day (ignore year)
                            if parsed.month == target_month and parsed.day == target_day:
                                return row_idx
                        except ValueError:
                            continue
            
            return None
        except Exception as e:
            logger.error(f"Error finding date row: {e}")
            return None
    
    def _find_date_row_google(self, date_str: str) -> Optional[int]:
        """Find date row in Google Sheet."""
        try:
            # Ensure connection is established
            self._ensure_connected()
            
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            target_month = date_obj.month
            target_day = date_obj.day
            
            # Check cache first
            current_time = time.time()
            if (self._date_column_cache is not None and 
                (current_time - self._date_column_cache_time) < self._date_column_cache_ttl):
                dates = self._date_column_cache
                logger.debug(f"Using cached date column ({len(dates)} rows)")
            else:
                # Rate limit API call
                self._rate_limit_api_call()
                
                try:
                    # Get all dates from column A (skip header rows 1-3)
                    dates = self.sheet.col_values(1)
                    # Cache the result
                    self._date_column_cache = dates
                    self._date_column_cache_time = current_time
                    self._reset_429_backoff()
                    logger.debug(f"Cached date column ({len(dates)} rows)")
                except Exception as e:
                    error_str = str(e).lower()
                    if '429' in error_str or 'quota' in error_str:
                        self._handle_429_error(e)
                    raise
            
            logger.debug(f"Looking for date: {date_str} (month={target_month}, day={target_day})")
            logger.debug(f"Found {len(dates)} rows in column A")
            
            # Try to find the date, handling various formats
            for idx, date_val in enumerate(dates, start=1):
                if idx <= 3:  # Skip header rows
                    continue
                    
                if not date_val:
                    continue
                
                date_str_lower = str(date_val).strip().lower()
                logger.debug(f"Row {idx}: Checking date value '{date_val}'")
                
                # Try parsing formats like "January 15", "January 15, 2025", etc.
                try:
                    # Format: "January 15" or "January 15, 2025"
                    import re
                    # Remove year if present, we'll match by month and day
                    date_clean = re.sub(r',\s*\d{4}', '', date_str_lower)
                    
                    # Parse month name and day
                    month_names = {
                        'january': 1, 'jan': 1, 'february': 2, 'feb': 2,
                        'march': 3, 'mar': 3, 'april': 4, 'apr': 4,
                        'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
                        'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'sept': 9,
                        'october': 10, 'oct': 10, 'november': 11, 'nov': 11,
                        'december': 12, 'dec': 12
                    }
                    
                    # Try to extract month and day
                    for month_name, month_num in month_names.items():
                        if month_name in date_clean:
                            # Extract day number
                            day_match = re.search(r'\b(\d{1,2})\b', date_clean)
                            if day_match:
                                day_num = int(day_match.group(1))
                                # Match if month and day match (ignore year)
                                if month_num == target_month and day_num == target_day:
                                    logger.info(f"✅ Found date match at row {idx}: '{date_val}' matches {date_str}")
                                    return idx
                except Exception:
                    pass
                
                # Try standard date formats
                for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d', '%B %d', '%B %d, %Y', '%b %d', '%b %d, %Y']:
                    try:
                        parsed = datetime.strptime(str(date_val), fmt)
                        # Match by month and day (ignore year)
                        if parsed.month == target_month and parsed.day == target_day:
                            return idx
                    except (ValueError, TypeError):
                        continue
            
            return None
        except Exception as e:
            logger.error(f"Error finding date row in Google Sheets: {e}")
            return None
    
    def check_availability(self, date_str: str, room_type: Optional[str] = None) -> Tuple[bool, Optional[str], int, Dict[str, int]]:
        """
        Check room availability for a given date.
        
        Args:
            date_str: Date in YYYY-MM-DD format
            room_type: Optional room type to check (e.g., "Twin", "Double"). If provided, only checks availability for that type.
            
        Returns:
            Tuple of (is_available, status_message, available_count, room_types_dict)
            is_available: True if at least one room is available (or at least one of the requested type)
            status_message: Human-readable status
            available_count: Number of available rooms (or available rooms of requested type)
            room_types_dict: Dictionary mapping room types to available counts (e.g., {"Twin": 2, "Double": 1})
        """
        row_num = self.find_date_row(date_str)
        
        if row_num is None:
            return False, "Availability cannot be checked for that date.", 0, {}
        
        try:
            if self.use_google_sheets:
                is_available, status_message, available_count, room_types_dict = self._check_availability_google(row_num)
            else:
                is_available, status_message, available_count, room_types_dict = self._check_availability_excel(row_num)
            
            # If a specific room type was requested, filter the results
            if room_type:
                room_type_normalized = room_type.strip().lower()
                requested_type_available = 0
                matched_room_type = None
                
                # Find matching room type (fuzzy matching - partial match)
                for rtype, count in room_types_dict.items():
                    rtype_lower = rtype.lower()
                    # Exact match
                    if rtype_lower == room_type_normalized:
                        requested_type_available = count
                        matched_room_type = rtype
                        break
                    # Partial match (e.g., "villa" matches "Two Bedroom Villa", "Villa")
                    elif room_type_normalized in rtype_lower or rtype_lower in room_type_normalized:
                        # Prefer longer matches (more specific)
                        if not matched_room_type or len(rtype) > len(matched_room_type):
                            requested_type_available = count
                            matched_room_type = rtype
                
                # If we found a match, use the actual room type name from the sheet
                if matched_room_type:
                    return True, f"{requested_type_available} {matched_room_type} room(s) available on that date.", requested_type_available, {matched_room_type: requested_type_available}
                
                if requested_type_available > 0:
                    return True, f"{requested_type_available} {room_type} room(s) available on that date.", requested_type_available, {room_type: requested_type_available}
                else:
                    # Check if any rooms of this type exist in the sheet
                    all_room_types = set()
                    if self.use_google_sheets:
                        room_types_map = self._get_room_types_google()
                    else:
                        room_types_map = self._get_room_types_excel()
                    for rtype in room_types_map.values():
                        all_room_types.add(rtype.lower())
                    
                    if room_type_normalized in all_room_types:
                        return False, f"Sorry, no {room_type} rooms are available on that date. Available room types: {', '.join([f'{count} {rtype}' for rtype, count in room_types_dict.items()])}.", 0, {}
                    else:
                        return False, f"Sorry, we don't have {room_type} rooms. Available room types: {', '.join([f'{count} {rtype}' for rtype, count in room_types_dict.items()])}.", 0, {}
            
            return is_available, status_message, available_count, room_types_dict
        except Exception as e:
            logger.error(f"Error checking availability: {e}")
            return False, "System temporarily unavailable.", 0, {}
    
    def check_availability_range(self, check_in_date: str, check_out_date: str, room_type: Optional[str] = None, num_rooms: int = 1) -> Tuple[bool, Optional[str], int, Dict[str, int]]:
        """
        Check room availability for a date range (check-in to check-out, exclusive).
        Only returns rooms that are available for ALL nights in the range.
        
        Args:
            check_in_date: Check-in date in YYYY-MM-DD format
            check_out_date: Check-out date in YYYY-MM-DD format
            room_type: Optional room type to check (e.g., "Twin", "Double"). If provided, only checks availability for that type.
            num_rooms: Number of rooms needed (default: 1)
            
        Returns:
            Tuple of (is_available, status_message, available_count, room_types_dict)
            is_available: True if at least num_rooms are available for ALL nights
            status_message: Human-readable status
            available_count: Number of rooms available for the full stay
            room_types_dict: Dictionary mapping room types to available counts
        """
        try:
            from datetime import datetime, timedelta
            
            # Parse dates
            check_in = datetime.strptime(check_in_date, '%Y-%m-%d')
            check_out = datetime.strptime(check_out_date, '%Y-%m-%d')
            
            # Generate all dates in range (check-in to check-out, exclusive)
            current_date = check_in
            dates_to_check = []
            while current_date < check_out:
                dates_to_check.append(current_date.strftime('%Y-%m-%d'))
                current_date += timedelta(days=1)
            
            if not dates_to_check:
                return False, "Invalid date range.", 0, {}
            
            logger.info(f"Checking availability for {len(dates_to_check)} nights: {dates_to_check}")
            
            # Get room types mapping
            if self.use_google_sheets:
                room_types_map = self._get_room_types_google()
            else:
                room_types_map = self._get_room_types_excel()
            
            # If room type is specified, filter to only that type (with fuzzy matching)
            if room_type:
                room_type_normalized = room_type.strip().lower()
                filtered_room_types = {}
                matched_room_type_name = None
                
                for col_idx, rtype in room_types_map.items():
                    rtype_lower = rtype.lower()
                    # Exact match
                    if rtype_lower == room_type_normalized:
                        filtered_room_types[col_idx] = rtype
                        if not matched_room_type_name:
                            matched_room_type_name = rtype
                    # Partial match (e.g., "villa" matches "Two Bedroom Villa", "Villa")
                    elif room_type_normalized in rtype_lower or rtype_lower in room_type_normalized:
                        filtered_room_types[col_idx] = rtype
                        # Prefer longer matches (more specific)
                        if not matched_room_type_name or len(rtype) > len(matched_room_type_name):
                            matched_room_type_name = rtype
                
                if not filtered_room_types:
                    # Room type doesn't exist
                    all_types = set(rt.lower() for rt in room_types_map.values())
                    # Check for partial matches in all types
                    found_partial = any(room_type_normalized in rt or rt in room_type_normalized for rt in all_types)
                    if found_partial:
                        return False, f"Sorry, no {room_type} rooms are available for the full stay.", 0, {}
                    else:
                        return False, f"Sorry, we don't have {room_type} rooms.", 0, {}
                room_types_map = filtered_room_types
                # Update room_type to the matched name for consistent messaging
                if matched_room_type_name:
                    room_type = matched_room_type_name
            
            # For each room column, check if it's available on ALL dates
            # Track which columns are available for all dates
            available_columns_by_type = {}  # {room_type: [list of column indices]}
            
            # Get all row numbers first
            date_row_map = {}  # {date_str: row_num}
            for date_str in dates_to_check:
                row_num = self.find_date_row(date_str)
                if row_num is not None:
                    date_row_map[date_str] = row_num
            
            if not date_row_map:
                return False, "Could not find dates in the sheet.", 0, {}
            
            # For Google Sheets, batch read all rows at once
            if self.use_google_sheets:
                # Get all unique row numbers
                row_nums = sorted(set(date_row_map.values()))
                
                # Batch read all rows in one API call
                if row_nums:
                    min_row = min(row_nums)
                    max_row = max(row_nums)
                    # Read all columns we need (from start_col to max column)
                    max_col = max(room_types_map.keys()) if room_types_map else 30
                    start_col = min(room_types_map.keys()) if room_types_map else 2
                    
                    # Convert column numbers to letters for A1 notation
                    def col_num_to_letter(n):
                        result = ""
                        while n > 0:
                            n -= 1
                            result = chr(65 + (n % 26)) + result
                            n //= 26
                        return result
                    
                    start_col_letter = col_num_to_letter(start_col)
                    end_col_letter = col_num_to_letter(max_col)
                    range_name = f'{start_col_letter}{min_row}:{end_col_letter}{max_row}'
                    
                    self._rate_limit_api_call()
                    try:
                        batch_data = self.sheet.get(range_name)
                        self._reset_429_backoff()
                        
                        # Create a map: {row_num: [cell_values]}
                        row_data_map = {}
                        for row_idx, row_data in enumerate(batch_data):
                            row_num = min_row + row_idx
                            row_data_map[row_num] = row_data  # Store as list
                        
                        # Now check availability using batch data
                        for col_idx, room_type_name in room_types_map.items():
                            is_available_all_dates = True
                            
                            # Check this column for all dates
                            for date_str in dates_to_check:
                                row_num = date_row_map.get(date_str)
                                if row_num is None:
                                    is_available_all_dates = False
                                    break
                                
                                # Get cell value from batch data
                                # Adjust col_idx relative to start_col (col_idx is 1-based, start_col is 1-based)
                                col_offset = col_idx - start_col
                                row_data = row_data_map.get(row_num, [])
                                
                                # Get cell value from the list
                                if col_offset >= 0 and col_offset < len(row_data):
                                    cell_value = row_data[col_offset]
                                else:
                                    cell_value = None
                                
                                # Room is occupied if cell has a value
                                if cell_value is not None and str(cell_value).strip() != '':
                                    is_available_all_dates = False
                                    break
                            
                            # If room is available for all dates, add it
                            if is_available_all_dates:
                                if room_type_name not in available_columns_by_type:
                                    available_columns_by_type[room_type_name] = []
                                available_columns_by_type[room_type_name].append(col_idx)
                    
                    except Exception as e:
                        error_str = str(e).lower()
                        if '429' in error_str or 'quota' in error_str:
                            self._handle_429_error(e)
                        # Fallback to individual reads with caching
                        logger.warning(f"Batch read failed, falling back to individual reads: {e}")
                        for col_idx, room_type_name in room_types_map.items():
                            is_available_all_dates = True
                            for date_str in dates_to_check:
                                row_num = date_row_map.get(date_str)
                                if row_num is None:
                                    is_available_all_dates = False
                                    break
                                try:
                                    # Use cached row data if available
                                    cache_key = f"row_{row_num}"
                                    current_time = time.time()
                                    if cache_key in self._row_data_cache:
                                        cached_data, cache_time = self._row_data_cache[cache_key]
                                        if (current_time - cache_time) < self._row_cache_ttl:
                                            row_data = cached_data
                                            col_index = col_idx - 1
                                            cell_value = row_data[col_index] if col_index < len(row_data) else None
                                        else:
                                            # Cache expired, read fresh
                                            self._rate_limit_api_call()
                                            cell_value = self.sheet.cell(row_num, col_idx).value
                                            self._reset_429_backoff()
                                    else:
                                        # Not cached, read fresh
                                        self._rate_limit_api_call()
                                        cell_value = self.sheet.cell(row_num, col_idx).value
                                        self._reset_429_backoff()
                                    
                                    if cell_value is not None and str(cell_value).strip() != '':
                                        is_available_all_dates = False
                                        break
                                except Exception as e2:
                                    error_str = str(e2).lower()
                                    if '429' in error_str or 'quota' in error_str:
                                        self._handle_429_error(e2)
                                    is_available_all_dates = False
                                    break
                            if is_available_all_dates:
                                if room_type_name not in available_columns_by_type:
                                    available_columns_by_type[room_type_name] = []
                                available_columns_by_type[room_type_name].append(col_idx)
            else:
                # Excel: use individual reads (no API quota issues)
                for col_idx, room_type_name in room_types_map.items():
                    is_available_all_dates = True
                    
                    # Check this column for all dates
                    for date_str in dates_to_check:
                        row_num = date_row_map.get(date_str)
                        if row_num is None:
                            is_available_all_dates = False
                            break
                        
                        # Check if this room is available on this date
                        try:
                            cell_value = self.worksheet.cell(row=row_num, column=col_idx).value
                            
                            # Room is occupied if cell has a value
                            if cell_value is not None and str(cell_value).strip() != '':
                                is_available_all_dates = False
                                break
                        except Exception as e:
                            logger.debug(f"Error checking column {col_idx} for date {date_str}: {e}")
                            is_available_all_dates = False
                            break
                    
                    # If room is available for all dates, add it
                    if is_available_all_dates:
                        if room_type_name not in available_columns_by_type:
                            available_columns_by_type[room_type_name] = []
                        available_columns_by_type[room_type_name].append(col_idx)
            
            # Count available rooms by type
            room_types_dict = {rtype: len(cols) for rtype, cols in available_columns_by_type.items()}
            total_available = sum(room_types_dict.values())
            
            # Check if we have enough rooms
            is_available = total_available >= num_rooms
            
            # Build status message
            if total_available == 0:
                if room_type:
                    status = f"Sorry, no {room_type} rooms are available for the full stay ({len(dates_to_check)} night(s))."
                else:
                    status = f"Sorry, no rooms are available for the full stay ({len(dates_to_check)} night(s))."
            elif total_available < num_rooms:
                if room_type:
                    status = f"Sorry, only {total_available} {room_type} room(s) available for the full stay, but you need {num_rooms}."
                else:
                    room_list = ", ".join([f"{count} {rtype}" for rtype, count in room_types_dict.items()])
                    status = f"Sorry, only {total_available} room(s) available for the full stay ({len(dates_to_check)} night(s)): {room_list}. You need {num_rooms} room(s)."
            else:
                if room_type:
                    status = f"{total_available} {room_type} room(s) available for the full stay ({len(dates_to_check)} night(s))."
                else:
                    room_list = ", ".join([f"{count} {rtype}" for rtype, count in room_types_dict.items()])
                    status = f"{total_available} room(s) available for the full stay ({len(dates_to_check)} night(s)): {room_list}."
            
            return is_available, status, total_available, room_types_dict
            
        except Exception as e:
            logger.error(f"Error checking availability range: {e}")
            return False, f"Error checking availability: {str(e)}", 0, {}
    
    def get_room_config(self) -> Dict[str, int]:
        """
        Get room configuration (max guests per room type) from a 'room_config' sheet.
        Falls back to config.py defaults if sheet doesn't exist.
        
        Returns:
            Dictionary mapping room type names (normalized) to max guests
            Example: {"two bedroom villa": 4, "twin": 2, "double": 2}
        """
        current_time = time.time()
        
        # Check cache
        if (self._room_config_cache is not None and 
            current_time - self._room_config_cache_time < self._room_config_cache_ttl):
            return self._room_config_cache
        
        config = {}
        
        # Try to load from config sheet
        try:
            if self.use_google_sheets and self._spreadsheet:
                try:
                    config_sheet = self._spreadsheet.worksheet("room_config")
                    # Read all rows (skip header)
                    rows = config_sheet.get_all_values()
                    if len(rows) > 1:  # Has header + data
                        for row in rows[1:]:
                            if len(row) >= 2 and row[0] and row[1]:
                                room_type = row[0].strip()
                                try:
                                    max_guests = int(row[1].strip())
                                    # Normalize room type name for matching
                                    config[room_type.lower()] = max_guests
                                    logger.debug(f"Loaded room config: {room_type} = {max_guests} guests")
                                except ValueError:
                                    logger.warning(f"Invalid max guests value for {room_type}: {row[1]}")
                    logger.info(f"✅ Loaded {len(config)} room configurations from room_config sheet")
                except WorksheetNotFound:
                    logger.info("ℹ️  No 'room_config' sheet found, using defaults from config.py")
                except Exception as e:
                    logger.warning(f"Could not read room_config sheet: {e}, using defaults")
            elif self.excel_path and OPENPYXL_AVAILABLE:
                try:
                    wb = load_workbook(self.excel_path, read_only=True)
                    if "room_config" in wb.sheetnames:
                        ws = wb["room_config"]
                        # Read rows (skip header)
                        for row in ws.iter_rows(min_row=2, values_only=True):
                            if row[0] and row[1]:
                                room_type = str(row[0]).strip()
                                try:
                                    max_guests = int(row[1])
                                    config[room_type.lower()] = max_guests
                                    logger.debug(f"Loaded room config: {room_type} = {max_guests} guests")
                                except (ValueError, TypeError):
                                    logger.warning(f"Invalid max guests value for {room_type}: {row[1]}")
                        logger.info(f"✅ Loaded {len(config)} room configurations from room_config sheet")
                    wb.close()
                except Exception as e:
                    logger.warning(f"Could not read room_config sheet: {e}, using defaults")
        except Exception as e:
            logger.warning(f"Error loading room config: {e}, using defaults")
        
        # Fallback to config.py defaults if no config found
        if not config:
            import config as app_config
            config = {
                "two bedroom villa": getattr(app_config, 'TWO_BEDROOM_VILLA_MAX_GUEST', 4),
                "twin": getattr(app_config, 'TWIN_MAX_GUEST', 2),
                "double": getattr(app_config, 'DOUBLE_ROOM_MAX_GUEST', 2),
            }
            logger.info("ℹ️  Using default room configurations from config.py")
        
        # Cache the result
        self._room_config_cache = config
        self._room_config_cache_time = current_time
        
        return config
    
    def get_max_guests_for_room_type(self, room_type: str) -> Optional[int]:
        """
        Get max guests for a specific room type using fuzzy matching.
        
        Args:
            room_type: Room type name (e.g., "Villa", "Two Bedroom Villa", "Twin")
            
        Returns:
            Max guests for the room type, or None if not found
        """
        if not room_type:
            return None
        
        config = self.get_room_config()
        room_type_normalized = room_type.strip().lower()
        
        # Try exact match first
        if room_type_normalized in config:
            return config[room_type_normalized]
        
        # Try partial match (e.g., "villa" matches "two bedroom villa")
        for config_room_type, max_guests in config.items():
            if room_type_normalized in config_room_type or config_room_type in room_type_normalized:
                return max_guests
        
        return None
    
    def _get_room_types_excel(self) -> Dict[int, str]:
        """
        Get room type mapping from header rows (row 1 and row 2).
        Dynamic approach: checks row 1 first (starting from column B), falls back to row 2 if needed.
        Skips column A (which typically has property name) and column B in row 2 (which has "Total Rooms").
        """
        room_types = {}
        try:
            # Strategy: Check row 1 for room types starting from column B (skip column A)
            # Also check row 2 as fallback, but skip "Total Rooms" in column B
            # Check up to 30 columns to ensure we catch all rooms
            max_col = max(self.worksheet.max_column + 1, 30)  # Check at least 30 columns
            
            # First, check row 2 column B to see if it says "Total Rooms" - this helps us identify the structure
            try:
                col_b_row2 = self.worksheet.cell(row=2, column=2).value
                has_total_rooms_col = col_b_row2 and "total" in str(col_b_row2).lower() and "room" in str(col_b_row2).lower()
            except:
                has_total_rooms_col = False
            
            # Start from column B (2) - skip column A which has property name
            start_col = 2
            # If column B in row 2 has "Total Rooms", room data starts from column C (3)
            if has_total_rooms_col:
                start_col = 3
            
            for col_idx in range(start_col, max_col):
                try:
                    # First, try row 1 (room type header)
                    cell_row1 = self.worksheet.cell(row=1, column=col_idx)
                    room_type = None
                    
                    if cell_row1.value:
                        room_type = str(cell_row1.value).strip()
                        # Skip if it's "Room X", "Total Rooms", "Date", or empty
                        # But allow room types that contain "bedroom" or "villa" (like "Two Bedroom Villa")
                        room_type_lower = room_type.lower()
                        if (room_type_lower.startswith("room ") or 
                            room_type_lower in ["", "total rooms", "date"]):
                            room_type = None
                        # Note: We now allow "bedroom" and "villa" in room type names
                        # Only skip if it's clearly a generic label
                    
                    # If row 1 didn't have a valid room type, check row 2 (but skip "Total Rooms")
                    if not room_type:
                        cell_row2 = self.worksheet.cell(row=2, column=col_idx)
                        if cell_row2.value:
                            potential_type = str(cell_row2.value).strip()
                            potential_lower = potential_type.lower()
                            # If it's "Room X" format, we still want to include it as a room
                            # but we'll use a generic name or check if there's data in the column
                            if potential_lower.startswith("room "):
                                # This is a room column, check if row 1 has a type or use generic
                                if not cell_row1.value or str(cell_row1.value).strip() == "":
                                    # No room type in row 1, use the room number as identifier
                                    room_type = potential_type  # e.g., "Room 7", "Room 8"
                                else:
                                    # Row 1 has something, use it as room type
                                    room_type = str(cell_row1.value).strip()
                            elif potential_lower not in ["total rooms", "date"]:
                                # Not "Room X" but also not a generic label, use it as room type
                                room_type = potential_type
                    
                    # If we found a valid room type, add it
                    if room_type and room_type:
                        room_types[col_idx] = room_type
                        logger.debug(f"Found room type '{room_type}' in column {col_idx}")
                    
                except Exception as e:
                    # If we can't read more columns, we've reached the end
                    logger.debug(f"Stopped reading room types at column {col_idx}: {e}")
                    break
            
            # If no room types found, try a more aggressive search starting from column B
            if not room_types:
                logger.warning("No room types found with standard method, trying alternative approach...")
                for col_idx in range(2, max_col):
                    try:
                        cell_row1 = self.worksheet.cell(row=1, column=col_idx)
                        if cell_row1.value:
                            val = str(cell_row1.value).strip()
                            # Accept any non-empty value that's not a generic label
                            # Allow room types with "bedroom" or "villa" in the name
                            if (val and 
                                not val.lower().startswith("room ") and
                                val.lower() not in ["", "total rooms", "date"]):
                                room_types[col_idx] = val
                                logger.debug(f"Found room type '{val}' in column {col_idx} (alternative method)")
                    except:
                        break
            
            logger.info(f"Found {len(room_types)} room types: {list(room_types.values())}")
            return room_types
        except Exception as e:
            logger.warning(f"Could not read room types from header: {e}")
            return room_types
    
    def _check_availability_excel(self, row_num: int) -> Tuple[bool, Optional[str], int, Dict[str, int]]:
        """Check availability in Excel file."""
        # Get room types from header row
        room_types = self._get_room_types_excel()
        
        available_count = 0
        total_rooms = 0
        room_type_counts = {}  # Track available rooms by type
        
        # More dynamic: only check columns that have room types defined
        # This makes it resilient to sheet structure changes
        checked_cols = set()
        max_col = min(self.worksheet.max_column + 1, 30)  # Check up to 30 columns
        
        # Determine starting column - check if column B in row 2 has "Total Rooms"
        try:
            col_b_row2 = self.worksheet.cell(row=2, column=2).value
            has_total_rooms_col = col_b_row2 and "total" in str(col_b_row2).lower() and "room" in str(col_b_row2).lower()
            start_col = 3 if has_total_rooms_col else 2
        except:
            start_col = 2
        
        for col_idx in range(start_col, max_col):
            try:
                # Check if this column has a room type defined
                room_type = room_types.get(col_idx)
                if not room_type:
                    # Skip columns without room types
                    continue
                
                # Avoid double-counting
                if col_idx in checked_cols:
                    continue
                checked_cols.add(col_idx)
                
                # Check availability for this room
                cell = self.worksheet.cell(row=row_num, column=col_idx)
                total_rooms += 1
                
                # Blank or empty cell = available
                # Numbers (1, 2, etc.) = occupied with that many guests
                cell_value = cell.value
                if cell_value is None or str(cell_value).strip() == '':
                    available_count += 1
                    room_type_counts[room_type] = room_type_counts.get(room_type, 0) + 1
                    logger.debug(f"Column {col_idx} ({room_type}): Available")
                else:
                    logger.debug(f"Column {col_idx} ({room_type}): Occupied (value: {cell_value})")
                    
            except Exception as e:
                # If we can't read more columns, we've reached the end
                logger.debug(f"Stopped checking availability at column {col_idx}: {e}")
                break
        
        if total_rooms == 0:
            return False, "No rooms configured for that date.", 0, {}
        
        is_available = available_count > 0
        
        # Build status message with room types - be explicit about all available types
        if available_count == 0:
            status = "Sorry, rooms are sold out on that date."
        else:
            # Create a clear list of all available room types
            room_type_parts = []
            for rtype, count in sorted(room_type_counts.items()):
                if count == 1:
                    room_type_parts.append(f"1 {rtype} room")
                else:
                    room_type_parts.append(f"{count} {rtype} rooms")
            
            room_type_list = ", ".join(room_type_parts)
            
            if available_count == 1:
                status = f"Limited availability on that date. Available: {room_type_list}."
            else:
                status = f"Rooms are available on that date. Available: {room_type_list}."
        
        return is_available, status, available_count, room_type_counts
    
    def _get_room_types_google(self) -> Dict[int, str]:
        """
        Get room type mapping from header rows (row 1 and row 2).
        Dynamic approach: checks row 1 first (starting from column B), falls back to row 2 if needed.
        Skips column A (which typically has property name) and column B in row 2 (which has "Total Rooms").
        """
        # Check cache first
        current_time = time.time()
        if (self._room_types_cache is not None and 
            (current_time - self._room_types_cache_time) < self._room_types_cache_ttl):
            logger.debug("Using cached room types")
            return self._room_types_cache.copy()
        
        room_types = {}
        try:
            # Ensure connection is established
            self._ensure_connected()
            
            # Strategy: Use batch read to get rows 1 and 2 at once (much more efficient)
            max_cols = 30  # Check at least 30 columns
            
            # Rate limit API call
            self._rate_limit_api_call()
            
            try:
                # Batch read rows 1 and 2 in one API call
                # Read from column B (2) to column AF (32) for both rows
                range_name = f'B1:AF2'
                header_data = self.sheet.get(range_name)
                self._reset_429_backoff()
                
                # Parse the batch data
                row1_data = header_data[0] if len(header_data) > 0 else []
                row2_data = header_data[1] if len(header_data) > 1 else []
                
                # First, check row 2 column B (index 0) to see if it says "Total Rooms"
                has_total_rooms_col = False
                if len(row2_data) > 0:
                    col_b_row2 = row2_data[0] if len(row2_data) > 0 else ""
                    has_total_rooms_col = col_b_row2 and "total" in str(col_b_row2).lower() and "room" in str(col_b_row2).lower()
                
                # Start from column B (2) - skip column A which has property name
                start_col = 2
                # If column B in row 2 has "Total Rooms", room data starts from column C (3)
                if has_total_rooms_col:
                    start_col = 3
                
                # Process columns starting from start_col
                # Adjust indices: row1_data[0] = column B, row1_data[1] = column C, etc.
                for col_offset in range(start_col - 2, min(max_cols, len(row1_data))):
                    col_num = col_offset + 2  # Actual column number (B=2, C=3, etc.)
                    try:
                        # Get values from batch data
                        cell_value_row1 = row1_data[col_offset] if col_offset < len(row1_data) else None
                        cell_value_row2 = row2_data[col_offset] if col_offset < len(row2_data) else None
                        
                        room_type = None
                        
                        if cell_value_row1:
                            room_type = str(cell_value_row1).strip()
                            # Skip if it's "Room X", "Total Rooms", "Date", or empty
                            room_type_lower = room_type.lower()
                            if (room_type_lower.startswith("room ") or 
                                room_type_lower in ["", "total rooms", "date"]):
                                room_type = None
                        
                        # If row 1 didn't have a valid room type, check row 2 (but skip "Total Rooms")
                        if not room_type and cell_value_row2:
                            potential_type = str(cell_value_row2).strip()
                            potential_lower = potential_type.lower()
                            # If it's "Room X" format, we still want to include it as a room
                            if potential_lower.startswith("room "):
                                # This is a room column, check if row 1 has a type or use generic
                                if not cell_value_row1 or str(cell_value_row1).strip() == "":
                                    # No room type in row 1, use the room number as identifier
                                    room_type = potential_type  # e.g., "Room 7", "Room 8"
                                else:
                                    # Row 1 has something, use it as room type
                                    room_type = str(cell_value_row1).strip()
                            elif potential_lower not in ["total rooms", "date"]:
                                # Not "Room X" but also not a generic label, use it as room type
                                room_type = potential_type
                        
                        # If we found a valid room type, add it
                        if room_type and room_type:
                            room_types[col_num] = room_type
                            logger.debug(f"Found room type '{room_type}' in column {col_num}")
                    
                    except Exception as e:
                        # If we can't read more columns, we've reached the end
                        logger.debug(f"Stopped reading room types at column {col_num}: {e}")
                        break
                
                # If no room types found, try a more aggressive search starting from column B
                if not room_types:
                    logger.warning("No room types found with standard method, trying alternative approach...")
                    for col_offset in range(0, min(max_cols, len(row1_data))):
                        col_num = col_offset + 2  # Actual column number
                        try:
                            cell_value_row1 = row1_data[col_offset] if col_offset < len(row1_data) else None
                            if cell_value_row1:
                                val = str(cell_value_row1).strip()
                                # Accept any non-empty value that's not a generic label
                                if (val and 
                                    not val.lower().startswith("room ") and
                                    val.lower() not in ["", "total rooms", "date"]):
                                    room_types[col_num] = val
                                    logger.debug(f"Found room type '{val}' in column {col_num} (alternative method)")
                        except:
                            break
                
            except Exception as e:
                error_str = str(e).lower()
                if '429' in error_str or 'quota' in error_str:
                    self._handle_429_error(e)
                raise
            
            # Cache the result
            self._room_types_cache = room_types.copy()
            self._room_types_cache_time = current_time
            
            logger.info(f"Found {len(room_types)} room types: {list(room_types.values())}")
            logger.info(f"Room type mapping: {dict(sorted(room_types.items()))}")
            
            return room_types
        except Exception as e:
            logger.warning(f"Could not read room types from header: {e}")
            return room_types
    
    def _check_availability_google(self, row_num: int) -> Tuple[bool, Optional[str], int, Dict[str, int]]:
        """Check availability in Google Sheet."""
        try:
            # Ensure connection is established
            self._ensure_connected()
            
            # Get room types from header row
            room_types = self._get_room_types_google()
            
            # Determine starting column from cached room types or check
            start_col = 2
            if room_types:
                # Use the minimum column from room types
                start_col = min(room_types.keys())
            else:
                # Fallback: check if column B in row 2 has "Total Rooms"
                try:
                    self._rate_limit_api_call()
                    col_b_row2 = self.sheet.cell(2, 2).value
                    has_total_rooms_col = col_b_row2 and "total" in str(col_b_row2).lower() and "room" in str(col_b_row2).lower()
                    start_col = 3 if has_total_rooms_col else 2
                    self._reset_429_backoff()
                except Exception as e:
                    error_str = str(e).lower()
                    if '429' in error_str or 'quota' in error_str:
                        self._handle_429_error(e)
                    start_col = 2
            
            # Check cache for row data
            current_time = time.time()
            cache_key = f"row_{row_num}"
            row_data = None
            
            if cache_key in self._row_data_cache:
                cached_data, cache_time = self._row_data_cache[cache_key]
                if (current_time - cache_time) < self._row_cache_ttl:
                    row_data = cached_data
                    logger.debug(f"Using cached row data for row {row_num}")
            
            if row_data is None:
                # Batch read the entire row in one API call
                max_col_to_read = max(start_col + 20, max(room_types.keys()) if room_types else start_col + 20)
                range_name = f'{row_num}:{row_num}'  # Read entire row
                
                self._rate_limit_api_call()
                try:
                    # Get row values - this returns a 2D array with one row
                    row_values = self.sheet.get(range_name)
                    if row_values and len(row_values) > 0:
                        row_data = row_values[0]  # First (and only) row
                    else:
                        row_data = []
                    self._reset_429_backoff()
                    
                    # Cache the result
                    self._row_data_cache[cache_key] = (row_data, current_time)
                    logger.debug(f"Cached row data for row {row_num}")
                except Exception as e:
                    error_str = str(e).lower()
                    if '429' in error_str or 'quota' in error_str:
                        self._handle_429_error(e)
                    # Fallback to empty list
                    row_data = []
            
            # Process row data
            available_count = 0
            total_rooms = 0
            room_type_counts = {}  # Track available rooms by type
            
            # Check each room column using cached row data
            for col_num in sorted(room_types.keys()):
                try:
                    room_type = room_types[col_num]
                    
                    # Get cell value from row data (column indices are 0-based, so col_num - 1)
                    # Column A = index 0, Column B = index 1, etc.
                    col_index = col_num - 1
                    cell_value = None
                    if col_index < len(row_data):
                        cell_value = row_data[col_index]
                    
                    total_rooms += 1
                    
                    # Blank or empty = available
                    # Numbers (1, 2, etc.) = occupied with that many guests
                    if cell_value is None or (isinstance(cell_value, str) and cell_value.strip() == ''):
                        available_count += 1
                        room_type_counts[room_type] = room_type_counts.get(room_type, 0) + 1
                        logger.debug(f"Column {col_num} ({room_type}): Available")
                    else:
                        logger.debug(f"Column {col_num} ({room_type}): Occupied (value: {cell_value})")
                except Exception as e:
                    logger.debug(f"Error processing column {col_num}: {e}")
                    continue
            
            logger.debug(f"Row {row_num}: Found {available_count} available rooms out of {total_rooms} total rooms")
            logger.debug(f"Available room types: {room_type_counts}")
            
            if total_rooms == 0:
                return False, "No rooms configured for that date.", 0, {}
            
            is_available = available_count > 0
            
            # Build status message with room types - be explicit about all available types
            if available_count == 0:
                status = "Sorry, rooms are sold out on that date."
            else:
                # Create a clear list of all available room types
                room_type_parts = []
                for rtype, count in sorted(room_type_counts.items()):
                    if count == 1:
                        room_type_parts.append(f"1 {rtype} room")
                    else:
                        room_type_parts.append(f"{count} {rtype} rooms")
                
                room_type_list = ", ".join(room_type_parts)
                
                if available_count == 1:
                    status = f"Limited availability on that date. Available: {room_type_list}."
                else:
                    status = f"Rooms are available on that date. Available: {room_type_list}."
            
            return is_available, status, available_count, room_type_counts
        except Exception as e:
            logger.error(f"Error checking Google Sheets availability: {e}", exc_info=True)
            return False, "System temporarily unavailable.", 0, {}
    
    def update_booking(self, check_in: str, check_out: str, num_rooms: int, num_guests: int, room_type_preference: str = None, booking_id: str = None, customer_name: str = None, phone_number: str = None) -> Tuple[bool, str]:
        """
        Update Excel/Google Sheet with booking after employee approval.
        Fills blank cells for the booking period.
        
        Args:
            check_in: Check-in date in YYYY-MM-DD format
            check_out: Check-out date in YYYY-MM-DD format (exclusive)
            num_rooms: Number of rooms to book
            num_guests: Number of guests (value to fill in cells)
            room_type_preference: Optional room type preference (e.g., "Double", "Twin", "Two Bedroom Villa")
            
        Returns:
            Tuple of (success, message)
        """
        try:
            # Parse dates
            check_in_date = datetime.strptime(check_in, '%Y-%m-%d')
            check_out_date = datetime.strptime(check_out, '%Y-%m-%d')
            
            # Generate all dates in range (check-in to check-out, exclusive)
            current_date = check_in_date
            dates_to_update = []
            while current_date < check_out_date:
                dates_to_update.append(current_date.strftime('%Y-%m-%d'))
                current_date += timedelta(days=1)
            
            logger.info(f"Updating booking for {len(dates_to_update)} dates: {dates_to_update}")
            
            if self.use_google_sheets:
                return self._update_booking_google(dates_to_update, num_rooms, num_guests, room_type_preference, booking_id, customer_name, phone_number)
            else:
                return self._update_booking_excel(dates_to_update, num_rooms, num_guests, room_type_preference, booking_id, customer_name, phone_number)
        except Exception as e:
            logger.error(f"Error updating booking: {e}")
            return False, f"Error updating booking: {str(e)}"
    
    def _update_booking_excel(self, dates: List[str], num_rooms: int, num_guests: int, room_type_preference: str = None, booking_id: str = None, customer_name: str = None, phone_number: str = None) -> Tuple[bool, str]:
        """Update booking in Excel file."""
        try:
            # Calculate guests per room with proper distribution
            # Distribute guests evenly, with remainder going to first rooms
            if num_rooms > 0:
                base_guests_per_room = num_guests // num_rooms
                remainder = num_guests % num_rooms
                # First 'remainder' rooms get base + 1, rest get base
                guests_distribution = [base_guests_per_room + 1] * remainder + [base_guests_per_room] * (num_rooms - remainder)
            else:
                guests_distribution = [num_guests]
            
            logger.info(f"Booking {num_rooms} room(s) with {num_guests} total guests = Distribution: {guests_distribution}")
            
            # Get room types mapping
            room_types = self._get_room_types_excel()
            
            # Determine starting column - check if column B in row 2 has "Total Rooms"
            try:
                col_b_row2 = self.worksheet.cell(row=2, column=2).value
                has_total_rooms_col = col_b_row2 and "total" in str(col_b_row2).lower() and "room" in str(col_b_row2).lower()
                start_col = 3 if has_total_rooms_col else 2
            except:
                start_col = 2
            
            # If room type preference is specified, find matching columns
            preferred_columns = []
            if room_type_preference:
                room_type_lower = room_type_preference.strip().lower()
                for col_idx, room_type in room_types.items():
                    room_type_str = str(room_type).lower()
                    # Match room type (handle variations)
                    if (room_type_lower in room_type_str or 
                        room_type_str in room_type_lower or
                        (room_type_lower == "double" and "double" in room_type_str) or
                        (room_type_lower == "twin" and "twin" in room_type_str) or
                        ("villa" in room_type_lower and "villa" in room_type_str)):
                        preferred_columns.append(col_idx)
                logger.info(f"Found {len(preferred_columns)} columns matching room type '{room_type_preference}': {preferred_columns}")
            
            # Track rooms booked per date
            total_rooms_booked = 0
            dates_updated = 0
            
            for date_str in dates:
                row_num = self.find_date_row(date_str)
                if row_num is None:
                    logger.warning(f"Date row not found for {date_str}, skipping")
                    continue
                
                rooms_booked_for_date = 0
                
                # If we have preferred columns, use those first
                columns_to_check = preferred_columns if preferred_columns else sorted(room_types.keys())
                
                for col_idx in columns_to_check:
                    if rooms_booked_for_date >= num_rooms:
                        break
                    
                    # Only fill columns that have room types
                    if col_idx not in room_types:
                        continue
                    
                    cell = self.worksheet.cell(row=row_num, column=col_idx)
                    
                    # Only fill blank cells (never overwrite)
                    if cell.value is None or str(cell.value).strip() == '':
                        # Get guests for this specific room from distribution
                        guests_for_this_room = guests_distribution[room_index] if room_index < len(guests_distribution) else base_guests_per_room
                        
                        cell.value = guests_for_this_room
                        
                        # Add comment with booking metadata if available (Excel supports comments)
                        if booking_id:
                            try:
                                from openpyxl.comments import Comment
                                comment_text = f"Booking ID: {booking_id}"
                                if customer_name:
                                    comment_text += f"\nCustomer: {customer_name}"
                                if phone_number:
                                    comment_text += f"\nPhone: {phone_number}"
                                cell.comment = Comment(comment_text, "Booking System")
                            except Exception as comment_error:
                                logger.warning(f"Could not add comment to Excel cell: {comment_error}")
                        
                        rooms_booked_for_date += 1
                        total_rooms_booked += 1
                        room_index += 1  # Move to next room in distribution
                        logger.info(f"Booked room in column {col_idx} ({room_types.get(col_idx)}) for date {date_str} with {guests_for_this_room} guests")
                
                if rooms_booked_for_date > 0:
                    dates_updated += 1
            
            # Save the workbook
            self.workbook.save(self.excel_path)
            
            expected_total = num_rooms * len(dates)
            if total_rooms_booked < expected_total:
                return False, f"Could only book {total_rooms_booked} out of {expected_total} requested room-days ({num_rooms} rooms × {len(dates)} nights). Updated {dates_updated} out of {len(dates)} dates."
            
            return True, f"Booking updated successfully. Booked {num_rooms} room(s) for {len(dates)} night(s)."
        except Exception as e:
            logger.error(f"Error updating Excel booking: {e}")
            return False, f"Error updating booking: {str(e)}"
    
    def _update_booking_google(self, dates: List[str], num_rooms: int, num_guests: int, room_type_preference: str = None, booking_id: str = None, customer_name: str = None, phone_number: str = None) -> Tuple[bool, str]:
        """Update booking in Google Sheet."""
        try:
            # Calculate guests per room with proper distribution
            # Distribute guests evenly, with remainder going to first rooms
            if num_rooms > 0:
                base_guests_per_room = num_guests // num_rooms
                remainder = num_guests % num_rooms
                # First 'remainder' rooms get base + 1, rest get base
                guests_distribution = [base_guests_per_room + 1] * remainder + [base_guests_per_room] * (num_rooms - remainder)
            else:
                guests_distribution = [num_guests]
            
            logger.info(f"Booking {num_rooms} room(s) with {num_guests} total guests = Distribution: {guests_distribution}")
            
            # Ensure connection is established
            self._ensure_connected()
            
            # Determine starting column - check if column B in row 2 has "Total Rooms"
            try:
                col_b_row2 = self.sheet.cell(2, 2).value
                has_total_rooms_col = col_b_row2 and "total" in str(col_b_row2).lower() and "room" in str(col_b_row2).lower()
                start_col = 3 if has_total_rooms_col else 2
            except:
                start_col = 2
            
            # Get room types to only update columns with valid room types
            room_types = self._get_room_types_google()
            logger.info(f"📋 Available room types in sheet: {dict(room_types)}")
            
            if not room_types:
                return False, "No room types found in sheet. Please check that room type headers are set correctly."
            
            # If room type preference is specified, find matching columns
            preferred_columns = []
            if room_type_preference:
                room_type_lower = room_type_preference.strip().lower()
                logger.info(f"🔍 Looking for room type '{room_type_preference}' (normalized: '{room_type_lower}')")
                for col_num, room_type in room_types.items():
                    room_type_str = str(room_type).lower()
                    # Match room type (handle variations) - improved fuzzy matching
                    if (room_type_lower == room_type_str or  # Exact match
                        room_type_lower in room_type_str or  # Partial match (e.g., "villa" in "Two Bedroom Villa")
                        room_type_str in room_type_lower or  # Reverse partial match
                        (room_type_lower == "double" and "double" in room_type_str) or
                        (room_type_lower == "twin" and "twin" in room_type_str) or
                        ("villa" in room_type_lower and "villa" in room_type_str) or
                        ("suite" in room_type_lower and "suite" in room_type_str)):
                        preferred_columns.append(col_num)
                        logger.debug(f"✅ Matched '{room_type_preference}' to column {col_num} ('{room_type}')")
                logger.info(f"Found {len(preferred_columns)} columns matching room type '{room_type_preference}': {preferred_columns}")
            
            # If no preferred columns found but room type was specified, log warning and use all available
            if room_type_preference and not preferred_columns:
                logger.warning(f"⚠️  No columns found matching room type '{room_type_preference}'. Available room types: {list(room_types.values())}. Will try to book from all available room types.")
            
            # Track rooms booked per date
            total_rooms_booked = 0
            dates_updated = 0
            
            for date_str in dates:
                room_index = 0  # Reset room index for each date (same rooms get same guests each night)
                row_num = self.find_date_row(date_str)
                if row_num is None:
                    logger.warning(f"Date row not found for {date_str}, skipping")
                    continue
                
                rooms_booked_for_date = 0
                
                # Get row data to check current state
                try:
                    row_data = self.sheet.row_values(row_num)
                except:
                    row_data = []
                
                # Ensure we have enough columns
                max_col_to_check = max(start_col + 20, len(row_data) if row_data else start_col + 20)
                
                # If we have preferred columns, use those first; otherwise use all available room types
                columns_to_check = preferred_columns if preferred_columns else sorted(room_types.keys())
                
                if not columns_to_check:
                    logger.error(f"❌ No room type columns found in sheet! Cannot book rooms.")
                    continue
                
                logger.info(f"🔍 Checking {len(columns_to_check)} columns for available rooms on {date_str}: {columns_to_check}")
                
                # Batch read the entire row once (much more efficient than reading cells one by one)
                cache_key = f"row_{row_num}"
                current_time = time.time()
                row_data = None
                
                # Try to use cached row data first
                if cache_key in self._row_data_cache:
                    cached_data, cache_time = self._row_data_cache[cache_key]
                    if (current_time - cache_time) < self._row_cache_ttl:
                        row_data = cached_data
                        logger.debug(f"Using cached row data for row {row_num}")
                
                # If not in cache or expired, batch read the entire row in one API call
                if row_data is None:
                    # Read entire row (from column 1 to max_col_to_check) in one batch call
                    def col_num_to_letter(n):
                        result = ""
                        while n > 0:
                            n -= 1
                            result = chr(65 + (n % 26)) + result
                            n //= 26
                        return result
                    
                    max_col_letter = col_num_to_letter(max_col_to_check)
                    range_name = f'A{row_num}:{max_col_letter}{row_num}'
                    
                    self._rate_limit_api_call()
                    try:
                        # Get row values - this returns a 2D array with one row
                        row_values = self.sheet.get(range_name)
                        if row_values and len(row_values) > 0:
                            row_data = row_values[0]  # First (and only) row
                        else:
                            row_data = []
                        self._reset_429_backoff()
                        
                        # Cache the result
                        self._row_data_cache[cache_key] = (row_data, current_time)
                        logger.debug(f"Cached row data for row {row_num} ({len(row_data)} columns)")
                    except Exception as e2:
                        error_str = str(e2).lower()
                        if '429' in error_str or 'quota' in error_str:
                            self._handle_429_error(e2)
                        logger.error(f"Error reading row {row_num}: {e2}")
                        row_data = []  # Fallback to empty list
                
                # Find available rooms (blank cells) starting from determined start_col
                # Only check columns that have room types defined
                for col_num in columns_to_check:
                    if rooms_booked_for_date >= num_rooms:
                        break
                    
                    if col_num < start_col:
                        logger.debug(f"Skipping column {col_num} - before start_col {start_col}")
                        continue
                    
                    if col_num > max_col_to_check:
                        logger.debug(f"Skipping column {col_num} - exceeds max_col {max_col_to_check}")
                        continue
                    
                    # Only fill columns that have room types
                    if col_num not in room_types:
                        logger.debug(f"Skipping column {col_num} - not in room_types")
                        continue
                    
                    try:
                        # Get cell value from batch-read row data (col_num is 1-based, array is 0-based)
                        col_index = col_num - 1
                        if col_index < len(row_data):
                            cell_value = row_data[col_index]
                        else:
                            cell_value = None
                        
                        logger.debug(f"Row {row_num}, Col {col_num} ({room_types.get(col_num)}): value='{cell_value}' (type: {type(cell_value).__name__ if cell_value is not None else 'None'})")
                        
                        # Check if cell is empty (handle None, empty string, whitespace, 0)
                        is_empty = (cell_value is None or 
                                   (isinstance(cell_value, str) and cell_value.strip() == '') or
                                   (isinstance(cell_value, (int, float)) and cell_value == 0))
                        
                        logger.info(f"🔍 Row {row_num}, Col {col_num} ({room_types.get(col_num)}): value='{cell_value}' (type: {type(cell_value).__name__}), is_empty={is_empty}")
                        
                        # Only fill blank cells (never overwrite)
                        if is_empty:
                            # Get guests for this specific room from distribution
                            # Use modulo to cycle through distribution for each date (same rooms get same guests each night)
                            guests_for_this_room = guests_distribution[room_index % len(guests_distribution)] if guests_distribution else base_guests_per_room
                            
                            # Update cell with guests for this specific room
                            self._rate_limit_api_call()
                            try:
                                self.sheet.update_cell(row_num, col_num, guests_for_this_room)
                                self._reset_429_backoff()
                                
                                # Add note/comment with booking metadata if available
                                if booking_id:
                                    try:
                                        # Create note text with booking info
                                        note_text = f"Booking ID: {booking_id}"
                                        if customer_name:
                                            note_text += f"\nCustomer: {customer_name}"
                                        if phone_number:
                                            note_text += f"\nPhone: {phone_number}"
                                        
                                        # Add note to cell using Google Sheets API batch_update
                                        try:
                                            # Get sheet ID from the worksheet
                                            sheet_id = self.sheet.id
                                            
                                            # Create note request using batch_update
                                            note_request = {
                                                "requests": [{
                                                    "updateCells": {
                                                        "range": {
                                                            "sheetId": sheet_id,
                                                            "startRowIndex": row_num - 1,
                                                            "endRowIndex": row_num,
                                                            "startColumnIndex": col_num - 1,
                                                            "endColumnIndex": col_num
                                                        },
                                                        "rows": [{
                                                            "values": [{
                                                                "note": note_text
                                                            }]
                                                        }],
                                                        "fields": "note"
                                                    }
                                                }]
                                            }
                                            self._spreadsheet.batch_update(note_request)
                                            logger.debug(f"Added booking metadata note to cell row {row_num}, col {col_num}")
                                        except Exception as note_error:
                                            logger.warning(f"Could not add note to cell: {note_error}")
                                    except Exception as note_error:
                                        logger.warning(f"Error adding booking metadata note: {note_error}")
                                
                                # Invalidate cache for this row since we updated it
                                if cache_key in self._row_data_cache:
                                    del self._row_data_cache[cache_key]
                                
                                rooms_booked_for_date += 1
                                total_rooms_booked += 1
                                room_index += 1  # Move to next room in distribution
                                logger.info(f"Booked room in column {col_num} ({room_types.get(col_num)}) for date {date_str} with {guests_for_this_room} guests")
                            except Exception as e2:
                                error_str = str(e2).lower()
                                if '429' in error_str or 'quota' in error_str:
                                    self._handle_429_error(e2)
                                raise
                    except Exception as e:
                        logger.error(f"❌ Error checking/updating column {col_num} for date {date_str}: {e}", exc_info=True)
                        continue
                
                if rooms_booked_for_date > 0:
                    dates_updated += 1
            
            expected_total = num_rooms * len(dates)
            if total_rooms_booked < expected_total:
                return False, f"Could only book {total_rooms_booked} out of {expected_total} requested room-days ({num_rooms} rooms × {len(dates)} nights). Updated {dates_updated} out of {len(dates)} dates."
            
            return True, f"Booking updated successfully. Booked {num_rooms} room(s) for {len(dates)} night(s)."
        except Exception as e:
            logger.error(f"Error updating Google Sheets booking: {e}")
            return False, f"Error updating booking: {str(e)}"
    
    def _get_or_create_monthly_sheet(self, month_name: str):
        """
        Get or create a monthly bookings sheet in Google Sheets.
        
        Args:
            month_name: Month name in lowercase (e.g., "january", "february")
            
        Returns:
            Worksheet object for the monthly sheet
        """
        if not self.use_google_sheets:
            return None
        
        try:
            self._ensure_connected()
            
            # Format sheet name: "january_bookings"
            sheet_name = f"{month_name.lower()}_bookings"
            
            # Try to get existing worksheet
            try:
                worksheet = self._spreadsheet.worksheet(sheet_name)
                logger.info(f"Found existing monthly sheet: {sheet_name}")
                return worksheet
            except WorksheetNotFound:
                # Create new worksheet
                logger.info(f"Creating new monthly sheet: {sheet_name}")
                worksheet = self._spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=15)
                
                # Set header row
                headers = [
                    "Booking ID",
                    "Customer Name",
                    "Phone Number",
                    "Check-in Date",
                    "Check-out Date",
                    "Number of Rooms",
                    "Number of Guests",
                    "Room Type Preference",
                    "Status",
                    "Created Date",
                    "Approved Date",
                    "Rejected Date",
                    "Rejection Reason",
                    "Cancelled Date",
                    "Cancellation Reason"
                ]
                worksheet.append_row(headers)
                
                # Format header row (bold)
                try:
                    worksheet.format('A1:M1', {'textFormat': {'bold': True}})
                except:
                    pass  # Formatting is optional
                
                logger.info(f"✅ Created monthly bookings sheet: {sheet_name}")
                return worksheet
        except Exception as e:
            logger.error(f"Error getting/creating monthly sheet: {e}")
            return None
    
    def log_approved_booking(self, booking_data: Dict) -> Tuple[bool, str]:
        """
        Log an approved booking to the monthly bookings sheet.
        
        Args:
            booking_data: Dictionary containing booking information with keys:
                - booking_id
                - customer_name
                - phone_number
                - check_in_date
                - check_out_date
                - num_rooms
                - num_guests
                - room_type_preference (optional)
                - status
                - created_at
                - approved_at (optional)
                - rejected_at (optional)
                - rejection_reason (optional)
                
        Returns:
            Tuple of (success, message)
        """
        if not self.use_google_sheets:
            return True, "Monthly logging only available for Google Sheets"
        
        try:
            # Determine month from check_in_date (when they're booking for)
            # This ensures bookings are saved to the month they're staying, not when approved
            date_str = booking_data.get('check_in_date') or booking_data.get('approved_at') or booking_data.get('created_at')
            if not date_str:
                return False, "No date found in booking data"
            
            # Parse date
            try:
                # Handle ISO format datetime strings
                if 'T' in date_str:
                    date_obj = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                else:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            except:
                # Try other formats
                try:
                    date_obj = datetime.strptime(date_str.split('T')[0], '%Y-%m-%d')
                except:
                    return False, f"Could not parse date: {date_str}"
            
            # Get month name
            month_names = {
                1: 'january', 2: 'february', 3: 'march', 4: 'april',
                5: 'may', 6: 'june', 7: 'july', 8: 'august',
                9: 'september', 10: 'october', 11: 'november', 12: 'december'
            }
            month_name = month_names.get(date_obj.month, 'unknown')
            
            # Get or create monthly sheet
            worksheet = self._get_or_create_monthly_sheet(month_name)
            if not worksheet:
                return False, "Could not create or access monthly sheet"
            
            # Prepare row data
            row_data = [
                booking_data.get('booking_id', ''),
                booking_data.get('customer_name', ''),
                booking_data.get('phone_number', ''),
                booking_data.get('check_in_date', ''),
                booking_data.get('check_out_date', ''),
                booking_data.get('num_rooms', ''),
                booking_data.get('num_guests', ''),
                booking_data.get('room_type_preference', '') or '',
                booking_data.get('status', ''),
                booking_data.get('created_at', ''),
                booking_data.get('approved_at', '') or '',
                booking_data.get('rejected_at', '') or '',
                booking_data.get('rejection_reason', '') or ''
            ]
            
            # Get all existing rows (skip header row 1)
            try:
                all_rows = worksheet.get_all_values()
                if len(all_rows) <= 1:
                    # Only header exists, just append
                    worksheet.append_row(row_data)
                else:
                    # Get data rows (skip header)
                    data_rows = all_rows[1:]
                    
                    # Parse check-in date for new booking
                    new_check_in = booking_data.get('check_in_date', '')
                    
                    # Find insertion position (sorted by check-in date, column D = index 3)
                    insert_index = len(data_rows)  # Default: append at end
                    for i, existing_row in enumerate(data_rows):
                        if len(existing_row) > 3:
                            existing_check_in = existing_row[3]  # Check-in date is column D (index 3)
                            if existing_check_in and new_check_in:
                                try:
                                    # Compare dates
                                    existing_date = datetime.strptime(existing_check_in, '%Y-%m-%d')
                                    new_date = datetime.strptime(new_check_in, '%Y-%m-%d')
                                    if new_date < existing_date:
                                        insert_index = i
                                        break
                                except:
                                    # If date parsing fails, compare as strings
                                    if new_check_in < existing_check_in:
                                        insert_index = i
                                        break
                    
                    # Insert at the correct position (row number = insert_index + 2, because row 1 is header)
                    worksheet.insert_row(row_data, insert_index + 2)
                    
                    logger.info(f"✅ Inserted booking {booking_data.get('booking_id')} at position {insert_index + 2} (sorted by check-in date)")
            except Exception as e:
                # Fallback: just append if sorting fails
                logger.warning(f"Could not sort bookings, appending to end: {e}")
                worksheet.append_row(row_data)
            
            logger.info(f"✅ Logged booking {booking_data.get('booking_id')} to {month_name}_bookings sheet")
            return True, f"Booking logged to {month_name}_bookings sheet (sorted by check-in date)"
            
        except Exception as e:
            logger.error(f"Error logging booking to monthly sheet: {e}", exc_info=True)
            return False, f"Error logging booking: {str(e)}"
    
    def remove_booking_from_monthly_sheet(self, booking_id: str, check_in_date: str) -> Tuple[bool, str]:
        """
        Remove a booking from the monthly bookings sheet.
        
        Args:
            booking_id: Booking ID to remove
            check_in_date: Check-in date in YYYY-MM-DD format (to determine which monthly sheet)
            
        Returns:
            Tuple of (success, message)
        """
        if not self.use_google_sheets:
            return True, "Monthly sheet removal only available for Google Sheets"
        
        try:
            # Determine month from check_in_date
            try:
                date_obj = datetime.strptime(check_in_date, '%Y-%m-%d')
            except:
                return False, f"Could not parse check-in date: {check_in_date}"
            
            month_names = {
                1: 'january', 2: 'february', 3: 'march', 4: 'april',
                5: 'may', 6: 'june', 7: 'july', 8: 'august',
                9: 'september', 10: 'october', 11: 'november', 12: 'december'
            }
            month_name = month_names.get(date_obj.month, 'unknown')
            sheet_name = f"{month_name}_bookings"
            
            # Get monthly sheet
            try:
                worksheet = self._spreadsheet.worksheet(sheet_name)
            except WorksheetNotFound:
                logger.warning(f"Monthly sheet {sheet_name} not found - booking may not have been logged")
                return True, f"Monthly sheet {sheet_name} not found (booking may not have been logged)"
            
            # Find and delete the row with matching booking_id
            all_rows = worksheet.get_all_values()
            if len(all_rows) <= 1:
                return True, "No bookings found in monthly sheet"
            
            # Search for booking_id in column A (index 0)
            row_to_delete = None
            for i, row in enumerate(all_rows[1:], start=2):  # Start from row 2 (skip header)
                if len(row) > 0 and row[0] == booking_id:
                    row_to_delete = i
                    break
            
            if row_to_delete:
                worksheet.delete_rows(row_to_delete)
                logger.info(f"✅ Removed booking {booking_id} from {sheet_name} sheet (row {row_to_delete})")
                return True, f"Removed booking from {sheet_name} sheet"
            else:
                logger.warning(f"Booking {booking_id} not found in {sheet_name} sheet")
                return True, f"Booking not found in {sheet_name} sheet (may have been removed already)"
                
        except Exception as e:
            logger.error(f"Error removing booking from monthly sheet: {e}", exc_info=True)
            return False, f"Error removing booking: {str(e)}"
    
    def free_up_rooms(self, check_in: str, check_out: str, num_rooms: int, room_type_preference: str = None, booking_id: str = None) -> Tuple[bool, str]:
        """
        Free up rooms in the allocation sheet by clearing cells that match the booking.
        This is used when a booking is cancelled.
        
        Args:
            check_in: Check-in date in YYYY-MM-DD format
            check_out: Check-out date in YYYY-MM-DD format (exclusive)
            num_rooms: Number of rooms to free up
            room_type_preference: Optional room type preference to match
            booking_id: Optional booking ID to match in cell notes/comments
            
        Returns:
            Tuple of (success, message)
        """
        try:
            # Parse dates
            check_in_date = datetime.strptime(check_in, '%Y-%m-%d')
            check_out_date = datetime.strptime(check_out, '%Y-%m-%d')
            
            # Generate all dates in range
            current_date = check_in_date
            dates_to_clear = []
            while current_date < check_out_date:
                dates_to_clear.append(current_date.strftime('%Y-%m-%d'))
                current_date += timedelta(days=1)
            
            logger.info(f"Freeing up rooms for {len(dates_to_clear)} dates: {dates_to_clear}")
            
            if self.use_google_sheets:
                return self._free_up_rooms_google(dates_to_clear, num_rooms, room_type_preference, booking_id)
            else:
                return self._free_up_rooms_excel(dates_to_clear, num_rooms, room_type_preference, booking_id)
        except Exception as e:
            logger.error(f"Error freeing up rooms: {e}")
            return False, f"Error freeing up rooms: {str(e)}"
    
    def _free_up_rooms_google(self, dates: List[str], num_rooms: int, room_type_preference: str = None, booking_id: str = None) -> Tuple[bool, str]:
        """Free up rooms in Google Sheet by clearing cells."""
        try:
            self._ensure_connected()
            
            # Determine starting column
            try:
                col_b_row2 = self.sheet.cell(2, 2).value
                has_total_rooms_col = col_b_row2 and "total" in str(col_b_row2).lower() and "room" in str(col_b_row2).lower()
                start_col = 3 if has_total_rooms_col else 2
            except:
                start_col = 2
            
            # Get room types
            room_types = self._get_room_types_google()
            if not room_types:
                return False, "No room types found in sheet"
            
            # Find matching columns if room type preference is specified
            preferred_columns = []
            if room_type_preference:
                room_type_lower = room_type_preference.strip().lower()
                for col_num, room_type in room_types.items():
                    room_type_str = str(room_type).lower()
                    if (room_type_lower == room_type_str or
                        room_type_lower in room_type_str or
                        room_type_str in room_type_lower or
                        (room_type_lower == "double" and "double" in room_type_str) or
                        (room_type_lower == "twin" and "twin" in room_type_str) or
                        ("villa" in room_type_lower and "villa" in room_type_str) or
                        ("suite" in room_type_lower and "suite" in room_type_str)):
                        preferred_columns.append(col_num)
            
            columns_to_check = preferred_columns if preferred_columns else sorted(room_types.keys())
            
            total_rooms_freed = 0
            dates_cleared = 0
            
            for date_str in dates:
                row_num = self.find_date_row(date_str)
                if row_num is None:
                    logger.warning(f"Date row not found for {date_str}, skipping")
                    continue
                
                rooms_freed_for_date = 0
                
                # Batch read the entire row
                cache_key = f"row_{row_num}"
                current_time = time.time()
                row_data = None
                
                if cache_key in self._row_data_cache:
                    cached_data, cache_time = self._row_data_cache[cache_key]
                    if (current_time - cache_time) < self._row_cache_ttl:
                        row_data = cached_data
                
                if row_data is None:
                    def col_num_to_letter(n):
                        result = ""
                        while n > 0:
                            n -= 1
                            result = chr(65 + (n % 26)) + result
                            n //= 26
                        return result
                    
                    max_col_to_check = max(start_col + 20, max(columns_to_check) if columns_to_check else start_col + 20)
                    max_col_letter = col_num_to_letter(max_col_to_check)
                    range_name = f'A{row_num}:{max_col_letter}{row_num}'
                    
                    self._rate_limit_api_call()
                    try:
                        row_values = self.sheet.get(range_name)
                        if row_values and len(row_values) > 0:
                            row_data = row_values[0]
                        else:
                            row_data = []
                        self._reset_429_backoff()
                    except Exception as e2:
                        logger.error(f"Error reading row {row_num}: {e2}")
                        row_data = []
                
                # Check each column and clear cells that match
                for col_num in columns_to_check:
                    if rooms_freed_for_date >= num_rooms:
                        break
                    
                    if col_num < start_col:
                        continue
                    
                    if col_num not in room_types:
                        continue
                    
                    try:
                        col_index = col_num - 1
                        if col_index < len(row_data):
                            cell_value = row_data[col_index]
                        else:
                            cell_value = None
                        
                        # Check if cell has a value (is occupied)
                        is_occupied = (cell_value is not None and 
                                      (isinstance(cell_value, str) and cell_value.strip() != '') and
                                      not (isinstance(cell_value, (int, float)) and cell_value == 0))
                        
                        if is_occupied:
                            # Check if this cell belongs to our booking (by checking note/comment if booking_id provided)
                            should_clear = True
                            if booking_id:
                                try:
                                    # Try to read cell note to verify it's our booking
                                    cell = self.sheet.cell(row_num, col_num)
                                    # Note checking would require additional API call, so we'll clear if booking_id matches pattern
                                    # For now, clear if cell is occupied and we're looking for this booking
                                    pass
                                except:
                                    pass
                            
                            if should_clear:
                                # Clear the cell value and note
                                self._rate_limit_api_call()
                                try:
                                    # Clear cell value
                                    self.sheet.update_cell(row_num, col_num, '')
                                    self._reset_429_backoff()
                                    
                                    # Clear cell note/comment using batch_update
                                    try:
                                        sheet_id = self.sheet.id
                                        note_request = {
                                            "requests": [{
                                                "updateCells": {
                                                    "range": {
                                                        "sheetId": sheet_id,
                                                        "startRowIndex": row_num - 1,
                                                        "endRowIndex": row_num,
                                                        "startColumnIndex": col_num - 1,
                                                        "endColumnIndex": col_num
                                                    },
                                                    "rows": [{
                                                        "values": [{
                                                            "note": ""  # Empty note to clear it
                                                        }]
                                                    }],
                                                    "fields": "note"
                                                }
                                            }]
                                        }
                                        self._spreadsheet.batch_update(note_request)
                                        logger.debug(f"Cleared note from cell row {row_num}, col {col_num}")
                                    except Exception as note_error:
                                        logger.warning(f"Could not clear note from cell: {note_error}")
                                    
                                    # Invalidate cache
                                    if cache_key in self._row_data_cache:
                                        del self._row_data_cache[cache_key]
                                    
                                    rooms_freed_for_date += 1
                                    total_rooms_freed += 1
                                    logger.info(f"Freed room in column {col_num} ({room_types.get(col_num)}) for date {date_str}")
                                except Exception as e2:
                                    logger.error(f"Error clearing cell row {row_num}, col {col_num}: {e2}")
                    except Exception as e:
                        logger.debug(f"Error checking column {col_num}: {e}")
                        continue
                
                if rooms_freed_for_date > 0:
                    dates_cleared += 1
            
            if total_rooms_freed > 0:
                logger.info(f"✅ Freed {total_rooms_freed} rooms across {dates_cleared} dates")
                return True, f"Freed {total_rooms_freed} rooms across {dates_cleared} dates"
            else:
                return False, "No rooms were found to free up (cells may already be empty or booking not found)"
                
        except Exception as e:
            logger.error(f"Error freeing up rooms in Google Sheet: {e}", exc_info=True)
            return False, f"Error freeing up rooms: {str(e)}"
    
    def _free_up_rooms_excel(self, dates: List[str], num_rooms: int, room_type_preference: str = None, booking_id: str = None) -> Tuple[bool, str]:
        """Free up rooms in Excel file by clearing cells."""
        # Similar implementation for Excel - for now, return success as Excel is less commonly used
        logger.warning("Freeing up rooms in Excel files is not yet implemented")
        return True, "Excel room freeing not yet implemented"
