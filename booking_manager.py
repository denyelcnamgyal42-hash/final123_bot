"""
Booking request management system.
Stores booking requests and handles employee approval workflow.
Uses Google Sheets for persistent storage (for Render free tier compatibility).
"""
import json
import os
import logging
from typing import Optional, Dict, List, Tuple
from datetime import datetime
from dataclasses import dataclass, asdict
from enum import Enum
import threading
import config

logger = logging.getLogger(__name__)

# Try to import Google Sheets support
try:
    import gspread
    from gspread.exceptions import WorksheetNotFound
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    logger.warning("gspread not available. Will use JSON file storage.")


class BookingStatus(Enum):
    """Booking request status."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class BookingRequest:
    """Represents a booking request."""
    booking_id: str
    customer_name: str
    phone_number: str
    check_in_date: str  # YYYY-MM-DD
    check_out_date: str  # YYYY-MM-DD
    num_rooms: int
    num_guests: int
    room_type_preference: str = None  # Optional room type preference (e.g., "Twin", "Double")
    status: str = BookingStatus.PENDING.value
    created_at: str = None
    approved_at: str = None
    rejected_at: str = None
    rejection_reason: str = None
    cancelled_at: str = None
    cancellation_reason: str = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict):
        return cls(**data)


class BookingManager:
    """Manages booking requests with persistence using Google Sheets or JSON file."""
    
    def __init__(self, storage_file: str = "bookings.json"):
        """
        Initialize booking manager.
        
        Args:
            storage_file: Path to JSON file for storing bookings (fallback only)
        """
        self.storage_file = storage_file
        self.lock = threading.RLock()
        self.bookings: Dict[str, BookingRequest] = {}
        self.use_google_sheets = False
        self._spreadsheet = None
        self._worksheet = None
        
        # Try to initialize Google Sheets if available
        if GSPREAD_AVAILABLE and config.GOOGLE_SHEET_ID:
            try:
                self._init_google_sheets()
            except Exception as e:
                logger.warning(f"Could not initialize Google Sheets for bookings: {e}. Using JSON file.")
                self.use_google_sheets = False
        
        self._load_bookings()
    
    def _init_google_sheets(self):
        """Initialize Google Sheets connection for bookings."""
        try:
            from google.oauth2.service_account import Credentials
            import gspread
            
            # Use the same credentials and sheet ID as the main Excel handler
            if not os.path.exists(config.GOOGLE_SHEETS_CREDENTIALS_PATH):
                raise FileNotFoundError("Google Sheets credentials not found")
            
            if not config.GOOGLE_SHEET_ID:
                raise ValueError("GOOGLE_SHEET_ID not configured")
            
            # Configure scopes
            scope = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.file"
            ]
            
            # Load credentials
            creds = Credentials.from_service_account_file(
                config.GOOGLE_SHEETS_CREDENTIALS_PATH,
                scopes=scope
            )
            
            # Create client
            client = gspread.authorize(creds)
            
            # Open spreadsheet
            self._spreadsheet = client.open_by_key(config.GOOGLE_SHEET_ID)
            
            # Get or create bookings sheet
            sheet_name = config.BOOKINGS_SHEET
            try:
                self._worksheet = self._spreadsheet.worksheet(sheet_name)
                logger.info(f"✅ Found existing bookings sheet: {sheet_name}")
            except WorksheetNotFound:
                # Create new sheet
                logger.info(f"Creating new bookings sheet: {sheet_name}")
                self._worksheet = self._spreadsheet.add_worksheet(
                    title=sheet_name, 
                    rows=1000, 
                    cols=15
                )
                
                # Set header row
                headers = [
                    "Booking ID", "Customer Name", "Phone Number",
                    "Check-in Date", "Check-out Date", "Number of Rooms",
                    "Number of Guests", "Room Type Preference", "Status",
                    "Created At", "Approved At", "Rejected At", "Rejection Reason",
                    "Cancelled At", "Cancellation Reason"
                ]
                self._worksheet.append_row(headers)
                
                # Format header row
                try:
                    self._worksheet.format('A1:O1', {'textFormat': {'bold': True}})
                except:
                    pass
                
                logger.info(f"✅ Created bookings sheet: {sheet_name}")
            
            self.use_google_sheets = True
            logger.info("✅ Using Google Sheets for booking storage")
            
        except Exception as e:
            logger.error(f"Failed to initialize Google Sheets: {e}")
            self.use_google_sheets = False
            raise
    
    def _load_bookings(self):
        """Load bookings from Google Sheets or JSON file."""
        if self.use_google_sheets and self._worksheet:
            try:
                # Get all rows (skip header)
                all_rows = self._worksheet.get_all_values()
                if len(all_rows) <= 1:
                    self.bookings = {}
                    logger.info("No bookings found in Google Sheets")
                    return
                
                data_rows = all_rows[1:]  # Skip header
                
                with self.lock:
                    self.bookings = {}
                    for row in data_rows:
                        if len(row) < 9 or not row[0]:  # Skip empty rows
                            continue
                        
                        try:
                            booking_data = {
                                'booking_id': row[0],
                                'customer_name': row[1] if len(row) > 1 else '',
                                'phone_number': row[2] if len(row) > 2 else '',
                                'check_in_date': row[3] if len(row) > 3 else '',
                                'check_out_date': row[4] if len(row) > 4 else '',
                                'num_rooms': int(row[5]) if len(row) > 5 and row[5] else 0,
                                'num_guests': int(row[6]) if len(row) > 6 and row[6] else 0,
                                'room_type_preference': row[7] if len(row) > 7 else None,
                                'status': row[8] if len(row) > 8 else 'pending',
                                'created_at': row[9] if len(row) > 9 else None,
                                'approved_at': row[10] if len(row) > 10 else None,
                                'rejected_at': row[11] if len(row) > 11 else None,
                                'rejection_reason': row[12] if len(row) > 12 else None,
                                'cancelled_at': row[13] if len(row) > 13 else None,
                                'cancellation_reason': row[14] if len(row) > 14 else None
                            }
                            
                            booking = BookingRequest.from_dict(booking_data)
                            self.bookings[booking.booking_id] = booking
                        except Exception as e:
                            logger.error(f"Error loading booking from row: {e}")
                            continue
                
                logger.info(f"Loaded {len(self.bookings)} bookings from Google Sheets")
            except Exception as e:
                logger.error(f"Error loading bookings from Google Sheets: {e}")
                self.bookings = {}
        else:
            # Fallback to JSON file
            if os.path.exists(self.storage_file):
                try:
                    with open(self.storage_file, 'r') as f:
                        data = json.load(f)
                    
                    with self.lock:
                        for booking_id, booking_data in data.items():
                            try:
                                booking = BookingRequest.from_dict(booking_data)
                                self.bookings[booking_id] = booking
                            except Exception as e:
                                logger.error(f"Error loading booking {booking_id}: {e}")
                                continue
                    
                    logger.info(f"Loaded {len(self.bookings)} bookings from JSON file")
                except Exception as e:
                    logger.error(f"Error loading bookings file: {e}")
                    self.bookings = {}
            else:
                self.bookings = {}
    
    def _save_bookings(self):
        """Save bookings to Google Sheets or JSON file."""
        if self.use_google_sheets and self._worksheet:
            try:
                # Get all existing rows
                all_rows = self._worksheet.get_all_values()
                header = all_rows[0] if all_rows else []
                existing_data = all_rows[1:] if len(all_rows) > 1 else []
                
                # Create a map of existing bookings by ID
                existing_ids = {row[0]: i for i, row in enumerate(existing_data) if row and len(row) > 0}
                
                # Prepare all booking rows
                booking_rows = []
                with self.lock:
                    for booking in self.bookings.values():
                        row_data = [
                            booking.booking_id,
                            booking.customer_name,
                            booking.phone_number,
                            booking.check_in_date,
                            booking.check_out_date,
                            str(booking.num_rooms),
                            str(booking.num_guests),
                            booking.room_type_preference or '',
                            booking.status,
                            booking.created_at or '',
                            booking.approved_at or '',
                            booking.rejected_at or '',
                            booking.rejection_reason or '',
                            getattr(booking, 'cancelled_at', None) or '',
                            getattr(booking, 'cancellation_reason', None) or ''
                        ]
                        booking_rows.append((booking.booking_id, row_data))
                
                # Update or insert rows
                for booking_id, row_data in booking_rows:
                    if booking_id in existing_ids:
                        # Update existing row (row number = existing_ids[booking_id] + 2, because row 1 is header)
                        row_num = existing_ids[booking_id] + 2
                        # Update with correct range (15 columns now with cancelled fields)
                        range_name = f'A{row_num}:O{row_num}'
                        self._worksheet.update(range_name, [row_data])
                    else:
                        # Insert new row (find correct position by check-in date for sorting)
                        insert_pos = len(existing_data) + 2  # Default: append
                        new_check_in = row_data[3]  # Check-in date
                        
                        for i, existing_row in enumerate(existing_data):
                            if len(existing_row) > 3 and existing_row[3]:
                                try:
                                    existing_date = datetime.strptime(existing_row[3], '%Y-%m-%d')
                                    new_date = datetime.strptime(new_check_in, '%Y-%m-%d')
                                    if new_date < existing_date:
                                        insert_pos = i + 2
                                        break
                                except:
                                    if new_check_in < existing_row[3]:
                                        insert_pos = i + 2
                                        break
                        
                        self._worksheet.insert_row(row_data, insert_pos)
                        existing_data.insert(insert_pos - 2, row_data)
                        # Update existing_ids for future updates
                        existing_ids[booking_id] = insert_pos - 2
                
                logger.info(f"✅ Saved {len(booking_rows)} bookings to Google Sheets")
            except Exception as e:
                logger.error(f"❌ Error saving bookings to Google Sheets: {e}", exc_info=True)
                # If save fails, try to reload and retry once
                try:
                    logger.info("🔄 Attempting to reload bookings sheet and retry save...")
                    self._init_google_sheets()
                    # Retry save
                    self._save_bookings()
                except Exception as retry_error:
                    logger.error(f"❌ Retry save also failed: {retry_error}", exc_info=True)
        else:
            # Fallback to JSON file
            with self.lock:
                data = {bid: booking.to_dict() for bid, booking in self.bookings.items()}
            
            try:
                # Atomic write
                temp_file = f"{self.storage_file}.tmp"
                with open(temp_file, 'w') as f:
                    json.dump(data, f, indent=2, default=str)
                
                os.replace(temp_file, self.storage_file)
            except Exception as e:
                logger.error(f"Error saving bookings: {e}")
    
    def create_booking_request(self, customer_name: str, phone_number: str,
                              check_in_date: str, check_out_date: str,
                              num_rooms: int, num_guests: int,
                              room_type_preference: str = None) -> BookingRequest:
        """
        Create a new booking request.
        
        Args:
            customer_name: Full name of customer
            phone_number: Customer phone number
            check_in_date: Check-in date (YYYY-MM-DD)
            check_out_date: Check-out date (YYYY-MM-DD)
            num_rooms: Number of rooms required
            num_guests: Number of guests
            room_type_preference: Optional room type preference (e.g., "Twin", "Double")
            
        Returns:
            BookingRequest object
            
        Raises:
            ValueError: If validation fails (too many rooms, too many guests, etc.)
        """
        # Validate max rooms per booking
        if num_rooms > config.MAX_ROOMS_PER_BOOKING:
            contact_msg = f"Please call us directly at {config.CONTACT_PHONE_NUMBER}" if config.CONTACT_PHONE_NUMBER else "Please call us directly"
            raise ValueError(
                f"Sorry, we can only process bookings for up to {config.MAX_ROOMS_PER_BOOKING} rooms through this chatbot. "
                f"For bookings with more than {config.MAX_ROOMS_PER_BOOKING} rooms, {contact_msg} to speak with our staff. "
                f"This helps us better assist travel agencies and group bookings."
            )
        
        # Check customer booking limit (max bookings per customer)
        customer_bookings = self.get_customer_bookings(phone_number)
        
        # Count only pending and approved bookings (rejected and cancelled don't count)
        active_bookings = [b for b in customer_bookings 
                          if b.status in [BookingStatus.PENDING.value, BookingStatus.APPROVED.value]]
        
        if len(active_bookings) >= config.MAX_BOOKINGS_PER_CUSTOMER:
            # Check if enough time has passed since last booking
            if active_bookings:
                # Sort by created_at to get the most recent
                sorted_bookings = sorted(active_bookings, 
                                       key=lambda b: b.created_at or '', 
                                       reverse=True)
                last_booking = sorted_bookings[0]
                
                if last_booking.created_at:
                    try:
                        # Parse the last booking's creation date
                        if 'T' in last_booking.created_at:
                            last_booking_date = datetime.fromisoformat(
                                last_booking.created_at.replace('Z', '+00:00')
                            )
                        else:
                            last_booking_date = datetime.strptime(last_booking.created_at, '%Y-%m-%d')
                        
                        # Calculate days since last booking
                        days_since_last = (datetime.now() - last_booking_date.replace(tzinfo=None)).days
                        
                        if days_since_last < config.BOOKING_COOLDOWN_DAYS:
                            days_remaining = config.BOOKING_COOLDOWN_DAYS - days_since_last
                            raise ValueError(
                                f"Sorry, you have reached the maximum of {config.MAX_BOOKINGS_PER_CUSTOMER} booking requests. "
                                f"You can make another booking request in {days_remaining} day(s). "
                                f"Please contact us directly at {config.CONTACT_PHONE_NUMBER} if you need immediate assistance."
                            )
                    except (ValueError, AttributeError) as e:
                        # If date parsing fails, still enforce the limit but don't mention cooldown
                        logger.warning(f"Could not parse last booking date: {e}")
                        raise ValueError(
                            f"Sorry, you have reached the maximum of {config.MAX_BOOKINGS_PER_CUSTOMER} booking requests. "
                            f"Please contact us directly at {config.CONTACT_PHONE_NUMBER} for additional bookings."
                        )
            else:
                # Shouldn't happen, but handle edge case
                raise ValueError(
                    f"Sorry, you have reached the maximum of {config.MAX_BOOKINGS_PER_CUSTOMER} booking requests. "
                    f"Please contact us directly at {config.CONTACT_PHONE_NUMBER} for additional bookings."
                )
        
        # Validate max guests per room type if room type preference is specified
        if room_type_preference:
            room_type_lower = room_type_preference.strip().lower()
            max_guests_per_room = None
            
            # Try to get max guests from Excel/Google Sheets config first
            try:
                from langchain_tools import excel_handler
                if excel_handler:
                    max_guests_per_room = excel_handler.get_max_guests_for_room_type(room_type_preference)
            except Exception as e:
                logger.debug(f"Could not get max guests from Excel config: {e}")
            
            # Fallback to config.py defaults if not found in Excel
            if max_guests_per_room is None:
                if "two bedroom villa" in room_type_lower or "two-bedroom villa" in room_type_lower or "villa" in room_type_lower:
                    max_guests_per_room = config.TWO_BEDROOM_VILLA_MAX_GUEST
                elif "twin" in room_type_lower:
                    max_guests_per_room = config.TWIN_MAX_GUEST
                elif "double" in room_type_lower:
                    max_guests_per_room = config.DOUBLE_ROOM_MAX_GUEST
            
            # If we found a max guest limit, validate
            if max_guests_per_room:
                max_total_guests = max_guests_per_room * num_rooms
                if num_guests > max_total_guests:
                    raise ValueError(
                        f"Sorry, {room_type_preference} rooms can accommodate a maximum of {max_guests_per_room} guests per room. "
                        f"For {num_rooms} room(s), the maximum number of guests is {max_total_guests}. "
                        f"You requested {num_guests} guests. Please adjust your booking or contact us for assistance."
                    )
        
        # Generate unique booking ID
        booking_id = f"BK{datetime.now().strftime('%Y%m%d%H%M%S')}{hash(phone_number) % 10000:04d}"
        
        booking = BookingRequest(
            booking_id=booking_id,
            customer_name=customer_name,
            phone_number=phone_number,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            num_rooms=num_rooms,
            num_guests=num_guests,
            room_type_preference=room_type_preference,
            status=BookingStatus.PENDING.value
        )
        
        with self.lock:
            self.bookings[booking_id] = booking
        
        # Save to Google Sheets/JSON with error handling
        try:
            self._save_bookings()
            logger.info(f"✅ Created and saved booking request: {booking_id}")
        except Exception as e:
            logger.error(f"❌ Failed to save booking {booking_id} to storage: {e}", exc_info=True)
            # Still return the booking even if save failed (it's in memory)
            # The booking will be lost on restart, but at least the user gets a response
            logger.warning(f"⚠️  Booking {booking_id} is in memory but not persisted. Check Google Sheets connection.")
        
        return booking
    
    def get_booking(self, booking_id: str) -> Optional[BookingRequest]:
        """Get booking by ID."""
        # Reload bookings from file to ensure we have the latest data
        self._load_bookings()
        with self.lock:
            return self.bookings.get(booking_id)
    
    def get_pending_bookings(self) -> List[BookingRequest]:
        """Get all pending booking requests."""
        # Reload bookings from file to ensure we have the latest data
        self._load_bookings()
        with self.lock:
            return [b for b in self.bookings.values() 
                   if b.status == BookingStatus.PENDING.value]
    
    def approve_booking(self, booking_id: str) -> Tuple[bool, str]:
        """
        Approve a booking request.
        
        Args:
            booking_id: Booking ID to approve
            
        Returns:
            Tuple of (success, message)
        """
        with self.lock:
            booking = self.bookings.get(booking_id)
            if not booking:
                return False, "Booking not found."
            
            if booking.status != BookingStatus.PENDING.value:
                return False, f"Booking is already {booking.status}."
            
            booking.status = BookingStatus.APPROVED.value
            booking.approved_at = datetime.now().isoformat()
        
        self._save_bookings()
        logger.info(f"Approved booking: {booking_id}")
        
        return True, "Booking approved successfully."
    
    def reject_booking(self, booking_id: str, reason: str = None) -> Tuple[bool, str]:
        """
        Reject a booking request.
        
        Args:
            booking_id: Booking ID to reject
            reason: Optional rejection reason
            
        Returns:
            Tuple of (success, message)
        """
        with self.lock:
            booking = self.bookings.get(booking_id)
            if not booking:
                return False, "Booking not found."
            
            if booking.status != BookingStatus.PENDING.value:
                return False, f"Booking is already {booking.status}."
            
            booking.status = BookingStatus.REJECTED.value
            booking.rejected_at = datetime.now().isoformat()
            booking.rejection_reason = reason
        
        self._save_bookings()
        logger.info(f"Rejected booking: {booking_id}")
        
        return True, "Booking rejected successfully."
    
    def cancel_booking(self, booking_id: str, reason: str = None) -> Tuple[bool, str]:
        """
        Cancel an approved booking.
        
        Args:
            booking_id: Booking ID to cancel
            reason: Optional cancellation reason
            
        Returns:
            Tuple of (success, message)
        """
        with self.lock:
            booking = self.bookings.get(booking_id)
            if not booking:
                return False, "Booking not found."
            
            if booking.status == BookingStatus.CANCELLED.value:
                return False, "Booking is already cancelled."
            
            if booking.status != BookingStatus.APPROVED.value:
                return False, f"Only approved bookings can be cancelled. Current status: {booking.status}."
            
            booking.status = BookingStatus.CANCELLED.value
            booking.cancelled_at = datetime.now().isoformat()
            booking.cancellation_reason = reason
        
        self._save_bookings()
        logger.info(f"Cancelled booking: {booking_id}")
        
        return True, "Booking cancelled successfully."
    
    def get_customer_bookings(self, phone_number: str) -> List[BookingRequest]:
        """Get all bookings for a customer."""
        with self.lock:
            return [b for b in self.bookings.values() 
                   if b.phone_number == phone_number]
    
    def get_all_bookings(self, status: Optional[str] = None) -> List[BookingRequest]:
        """Get all bookings, optionally filtered by status."""
        # Reload bookings from file to ensure we have the latest data
        self._load_bookings()
        with self.lock:
            if status:
                return [b for b in self.bookings.values() if b.status == status]
            return list(self.bookings.values())
    
    def reload_bookings(self):
        """Manually reload bookings from storage file."""
        self._load_bookings()
