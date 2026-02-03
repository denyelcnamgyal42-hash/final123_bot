"""
LangChain tools for the hotel booking chatbot.
"""
import logging
from typing import Optional, Dict, Any
from langchain.tools import tool
from date_parser import DateParser
from excel_handler import ExcelHandler
from booking_manager import BookingManager
import config
import threading

logger = logging.getLogger(__name__)

# Thread-local storage for current session phone number
_thread_local = threading.local()

# Initialize components
date_parser = DateParser()
booking_manager = BookingManager()

# Initialize Excel handler based on config
excel_handler = None
try:
    if config.GOOGLE_SHEET_ID:
        logger.info(f"🔧 Initializing Google Sheets connection...")
        logger.info(f"   Sheet ID: {config.GOOGLE_SHEET_ID}")
        logger.info(f"   Credentials: {config.GOOGLE_SHEETS_CREDENTIALS_PATH}")
        logger.info(f"   Worksheet name: {config.HOTELS_SHEET}")
        
        # Check if credentials file exists (it may have been created from CREDENTIALS_JSON)
        import os
        if not os.path.exists(config.GOOGLE_SHEETS_CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"Credentials file not found at: {config.GOOGLE_SHEETS_CREDENTIALS_PATH}\n"
                f"Please either:\n"
                f"1. Set CREDENTIALS_JSON environment variable with your credentials.json content, OR\n"
                f"2. Upload credentials.json file and set GOOGLE_SHEETS_CREDENTIALS_PATH"
            )
        
        excel_handler = ExcelHandler(
            google_sheet_id=config.GOOGLE_SHEET_ID,
            google_credentials_path=config.GOOGLE_SHEETS_CREDENTIALS_PATH,
            sheet_name=config.HOTELS_SHEET
        )
        logger.info("✅ Google Sheets handler initialized successfully!")
    else:
        raise ValueError(
            "GOOGLE_SHEET_ID is required. This application only supports Google Sheets.\n"
            "Please set GOOGLE_SHEET_ID in your environment variables or .env file."
        )
except FileNotFoundError as e:
    logger.error(f"❌ File not found: {e}")
    logger.warning("Excel handler not available. Availability checking and booking updates will not work.")
    excel_handler = None
except ValueError as e:
    logger.error(f"❌ Configuration error: {e}")
    logger.warning("Excel handler not available. Availability checking and booking updates will not work.")
    logger.warning("\n📋 To fix Google Sheets connection:")
    logger.warning("   1. Make sure GOOGLE_SHEET_ID is set in your .env file")
    logger.warning("   2. Share your Google Sheet with the service account email (shown in error above)")
    logger.warning("   3. Give the service account 'Editor' permissions")
    excel_handler = None
except Exception as e:
    logger.error(f"❌ Failed to initialize Excel handler: {e}")
    logger.warning("Excel handler not available. Availability checking and booking updates will not work.")
    import traceback
    logger.error("Full error traceback:")
    traceback.print_exc()
    excel_handler = None


@tool
def parse_date(date_string: str) -> Dict[str, Any]:
    """
    Parse a human-readable date string into normalized YYYY-MM-DD format.
    
    Supports formats like:
    - "21 January", "21st January", "January 21"
    - "Jan 21", "21 Jan"
    - "27" (assumes current month)
    - "tomorrow", "day after tomorrow"
    - "next Friday", "coming Monday", "this Sunday"
    - "coming weekend", "next weekend", "the following weekend"
    - "21/01/2024", "21-01-2024"
    
    Args:
        date_string: The date string to parse
        
    Returns:
        Dictionary with:
        - "success": bool
        - "date": YYYY-MM-DD format if successful, None otherwise
        - "error": Error message if parsing failed, None otherwise
    """
    try:
        normalized_date, error = date_parser.parse_date(date_string)
        
        if normalized_date:
            return {
                "success": True,
                "date": normalized_date,
                "error": None
            }
        else:
            return {
                "success": False,
                "date": None,
                "error": error or "Could not parse date."
            }
    except Exception as e:
        logger.error(f"Error in parse_date tool: {e}")
        return {
            "success": False,
            "date": None,
            "error": f"Error parsing date: {str(e)}"
        }


@tool
def check_room_availability(date_string: str, room_type: str = "") -> Dict[str, Any]:
    """
    Check room availability for a specific date.
    
    The date should be in YYYY-MM-DD format. If you receive a human-readable date,
    use parse_date tool first to normalize it.
    
    Args:
        date_string: Date in YYYY-MM-DD format
        room_type: Optional room type to check (e.g., "Twin", "Double"). If provided, only checks availability for that specific type.
        
    Returns:
        Dictionary with:
        - "success": bool
        - "available": bool (True if at least one room is available, or at least one of requested type)
        - "status_message": Human-readable status message (includes room types)
        - "available_count": Number of available rooms (or available rooms of requested type)
        - "room_types": Dictionary mapping room types to available counts (e.g., {"Twin": 2, "Double": 1})
        - "error": Error message if check failed
    """
    if not excel_handler:
        return {
            "success": False,
            "available": False,
            "status_message": "I'm sorry, but the room availability system is not currently configured. Please contact the hotel directly to check availability, or you can still make a booking request and our staff will check availability for you.",
            "available_count": 0,
            "room_types": {},
            "error": "Excel handler not initialized - availability system needs to be configured"
        }
    
    try:
        room_type_to_check = room_type.strip() if room_type else None
        is_available, status_message, available_count, room_types = excel_handler.check_availability(date_string, room_type_to_check)
        
        return {
            "success": True,
            "available": is_available,
            "status_message": status_message,
            "available_count": available_count,
            "room_types": room_types,
            "error": None
        }
    except Exception as e:
        logger.error(f"Error checking availability: {e}")
        return {
            "success": False,
            "available": False,
            "status_message": "I'm sorry, but I'm unable to check availability right now. You can still make a booking request and our staff will check availability and contact you to confirm.",
            "available_count": 0,
            "room_types": {},
            "error": str(e)
        }


@tool
def check_room_availability_range(check_in_date: str, check_out_date: str, room_type: str = "", num_rooms: int = 1) -> Dict[str, Any]:
    """
    Check room availability for a date range (multi-night stay).
    This checks if rooms are available for ALL nights from check-in to check-out.
    Use this when the customer mentions staying for multiple nights.
    
    Dates should be in YYYY-MM-DD format. If you receive human-readable dates,
    use parse_date tool first to normalize them.
    
    Args:
        check_in_date: Check-in date in YYYY-MM-DD format
        check_out_date: Check-out date in YYYY-MM-DD format (exclusive - the night before check-out is the last night)
        room_type: Optional room type to check (e.g., "Twin", "Double"). If provided, only checks availability for that specific type.
        num_rooms: Number of rooms needed (default: 1)
        
    Returns:
        Dictionary with:
        - "success": bool
        - "available": bool (True if at least num_rooms are available for ALL nights)
        - "status_message": Human-readable status message
        - "available_count": Number of rooms available for the full stay
        - "room_types": Dictionary mapping room types to available counts
        - "error": Error message if check failed
    """
    if not excel_handler:
        return {
            "success": False,
            "available": False,
            "status_message": "I'm sorry, but the room availability system is not currently configured. Please contact the hotel directly to check availability, or you can still make a booking request and our staff will check availability for you.",
            "available_count": 0,
            "room_types": {},
            "error": "Excel handler not initialized - availability system needs to be configured"
        }
    
    try:
        room_type_to_check = room_type.strip() if room_type else None
        is_available, status_message, available_count, room_types = excel_handler.check_availability_range(
            check_in_date, check_out_date, room_type_to_check, num_rooms
        )
        
        return {
            "success": True,
            "available": is_available,
            "status_message": status_message,
            "available_count": available_count,
            "room_types": room_types,
            "error": None
        }
    except Exception as e:
        logger.error(f"Error checking availability range: {e}")
        return {
            "success": False,
            "available": False,
            "status_message": "I'm sorry, but I'm unable to check availability right now. You can still make a booking request and our staff will check availability and contact you to confirm.",
            "available_count": 0,
            "room_types": {},
            "error": str(e)
        }


@tool
def create_booking_request(customer_name: str, phone_number: str = "",
                          check_in_date: str = "", check_out_date: str = "",
                          num_rooms: int = 1, num_guests: int = 1,
                          room_type_preference: str = "") -> Dict[str, Any]:
    """
    Create a booking request (not a confirmed booking).
    This will be sent to hotel staff for manual approval.
    
    SECURITY: The phone_number parameter is IGNORED for security. The tool automatically
    uses the phone number from the session context to ensure bookings are created for
    the correct customer.
    
    Args:
        customer_name: Full name of the customer
        phone_number: IGNORED - This parameter is ignored for security. The tool automatically
                     uses the phone number from the session context.
        check_in_date: Check-in date in YYYY-MM-DD format
        check_out_date: Check-out date in YYYY-MM-DD format
        num_rooms: Number of rooms required (maximum 3 rooms allowed through chatbot)
        num_guests: Total number of guests
        room_type_preference: Optional room type preference (e.g., "Twin", "Double", "Two Bedroom Villa"). Leave empty if no preference.
        
    Returns:
        Dictionary with:
        - "success": bool
        - "booking_id": Booking ID if successful
        - "message": Success or error message
    """
    try:
        # SECURITY: Always use the session phone number, never trust the parameter
        session_phone = get_session_phone_number()
        if not session_phone:
            logger.error("create_booking_request called without session phone number context")
            return {
                "success": False,
                "booking_id": None,
                "message": "I'm sorry, but I cannot create a booking request at this time. Please try again."
            }
        
        # Validate required fields
        if not check_in_date or not check_out_date:
            return {
                "success": False,
                "booking_id": None,
                "message": "I need both check-in and check-out dates to create your booking request. Please provide both dates."
            }
        
        if not customer_name or customer_name.strip() == "":
            return {
                "success": False,
                "booking_id": None,
                "message": "I need your name to create the booking request. Please provide your full name."
            }
        
        # SECURITY: Validate numeric inputs to prevent negative numbers or invalid values
        if not isinstance(num_rooms, int) or num_rooms < 1:
            return {
                "success": False,
                "booking_id": None,
                "message": "Number of rooms must be at least 1. Please provide a valid number of rooms."
            }
        
        if not isinstance(num_guests, int) or num_guests < 1:
            return {
                "success": False,
                "booking_id": None,
                "message": "Number of guests must be at least 1. Please provide a valid number of guests."
            }
        
        # SECURITY: Validate date format (YYYY-MM-DD)
        try:
            from datetime import datetime
            datetime.strptime(check_in_date, '%Y-%m-%d')
            datetime.strptime(check_out_date, '%Y-%m-%d')
        except ValueError:
            return {
                "success": False,
                "booking_id": None,
                "message": "Invalid date format. Dates must be in YYYY-MM-DD format."
            }
        
        # SECURITY: Validate that check-out is after check-in
        try:
            from datetime import datetime
            check_in = datetime.strptime(check_in_date, '%Y-%m-%d')
            check_out = datetime.strptime(check_out_date, '%Y-%m-%d')
            if check_out <= check_in:
                return {
                    "success": False,
                    "booking_id": None,
                    "message": "Check-out date must be after check-in date. Please provide valid dates."
                }
        except Exception:
            pass  # Already validated above
        
        # SECURITY: Sanitize customer name (remove potentially dangerous characters, limit length)
        customer_name = customer_name.strip()[:100]  # Limit to 100 characters
        
        room_pref = room_type_preference.strip()[:50] if room_type_preference else None  # Limit room type to 50 chars
        booking = booking_manager.create_booking_request(
            customer_name=customer_name,
            phone_number=session_phone,  # Use session phone number, ignore parameter
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            num_rooms=num_rooms,
            num_guests=num_guests,
            room_type_preference=room_pref
        )
        
        return {
            "success": True,
            "booking_id": booking.booking_id,
            "message": f"✅ Perfect! Your booking request has been submitted successfully.\n\n📋 Booking ID: {booking.booking_id}\n\nOur team will review your request and contact you shortly to confirm your booking. We look forward to hosting you! 😊"
        }
    except ValueError as e:
        # Validation errors (max rooms, max guests, etc.)
        logger.warning(f"Booking validation failed: {e}")
        return {
            "success": False,
            "booking_id": None,
            "message": str(e)
        }
    except Exception as e:
        logger.error(f"Error creating booking request: {e}")
        return {
            "success": False,
            "booking_id": None,
            "message": f"Error creating booking request: {str(e)}"
        }


def set_session_phone_number(phone_number: str):
    """Set the current session's phone number for thread-local access."""
    _thread_local.phone_number = phone_number

def get_session_phone_number() -> Optional[str]:
    """Get the current session's phone number from thread-local storage."""
    return getattr(_thread_local, 'phone_number', None)

def clear_session_phone_number():
    """Clear the session phone number from thread-local storage (security: prevents data leakage)."""
    if hasattr(_thread_local, 'phone_number'):
        delattr(_thread_local, 'phone_number')

@tool
def check_booking_status(phone_number: str = "") -> Dict[str, Any]:
    """
    Check the status of booking requests for the CURRENT customer only.
    
    SECURITY: This tool ONLY shows bookings for the customer who is currently messaging.
    It automatically uses the phone number from the session context, NOT any phone number
    provided as a parameter. Customers can ONLY view their own bookings - this is a 
    privacy and security requirement.
    
    IMPORTANT: The phone_number parameter is IGNORED for security. The tool automatically
    uses the phone number from the session context. If a customer asks about someone else's 
    booking, politely decline and explain that each customer can only view their own bookings.
    
    Args:
        phone_number: IGNORED - This parameter is ignored for security. The tool automatically
                     uses the phone number from the session context.
        
    Returns:
        Dictionary with:
        - "success": bool
        - "bookings": List of booking dictionaries (only for the current customer)
        - "message": Status message
    """
    try:
        # SECURITY: Always use the session phone number, never trust the parameter
        session_phone = get_session_phone_number()
        if not session_phone:
            logger.error("check_booking_status called without session phone number context")
            return {
                "success": False,
                "bookings": [],
                "message": "I'm sorry, but I cannot retrieve booking information at this time. Please try again."
            }
        
        # Use session phone number, ignore any phone number from the parameter
        bookings = booking_manager.get_customer_bookings(session_phone)
        
        if not bookings:
            return {
                "success": True,
                "bookings": [],
                "message": "You have no booking requests."
            }
        
        booking_list = []
        for booking in bookings:
            booking_list.append({
                "booking_id": booking.booking_id,
                "check_in": booking.check_in_date,
                "check_out": booking.check_out_date,
                "num_rooms": booking.num_rooms,
                "num_guests": booking.num_guests,
                "room_type": booking.room_type_preference or "Any available",
                "status": booking.status,
                "created_at": booking.created_at,
                "approved_at": booking.approved_at,
                "rejected_at": booking.rejected_at
            })
        
        return {
            "success": True,
            "bookings": booking_list,
            "message": f"You have {len(bookings)} booking request(s)."
        }
    except Exception as e:
        logger.error(f"Error checking booking status: {e}")
        return {
            "success": False,
            "bookings": [],
            "message": f"Error checking booking status: {str(e)}"
        }
