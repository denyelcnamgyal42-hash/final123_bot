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

logger = logging.getLogger(__name__)

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
        
        # Check if credentials file exists
        import os
        if not os.path.exists(config.GOOGLE_SHEETS_CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"Credentials file not found at: {config.GOOGLE_SHEETS_CREDENTIALS_PATH}\n"
                f"Please check the GOOGLE_SHEETS_CREDENTIALS_PATH in your .env file."
            )
        
        excel_handler = ExcelHandler(
            google_sheet_id=config.GOOGLE_SHEET_ID,
            google_credentials_path=config.GOOGLE_SHEETS_CREDENTIALS_PATH,
            sheet_name=config.HOTELS_SHEET
        )
        logger.info("✅ Google Sheets handler initialized successfully!")
    else:
        logger.info(f"📄 Using local Excel file: {config.EXCEL_PATH}")
        # Use local Excel file
        excel_handler = ExcelHandler(
            excel_path=config.EXCEL_PATH,
            sheet_name=config.HOTELS_SHEET
        )
        logger.info("✅ Excel handler initialized successfully!")
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
    - "next Friday"
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
def create_booking_request(customer_name: str, phone_number: str,
                          check_in_date: str, check_out_date: str,
                          num_rooms: int, num_guests: int,
                          room_type_preference: str = "") -> Dict[str, Any]:
    """
    Create a booking request (not a confirmed booking).
    This will be sent to hotel staff for manual approval.
    
    Args:
        customer_name: Full name of the customer
        phone_number: Customer's phone number
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
        room_pref = room_type_preference.strip() if room_type_preference else None
        booking = booking_manager.create_booking_request(
            customer_name=customer_name,
            phone_number=phone_number,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            num_rooms=num_rooms,
            num_guests=num_guests,
            room_type_preference=room_pref
        )
        
        return {
            "success": True,
            "booking_id": booking.booking_id,
            "message": f"Your booking request has been submitted. Booking ID: {booking.booking_id}. Our staff will contact you shortly to confirm."
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


@tool
def check_booking_status(phone_number: str) -> Dict[str, Any]:
    """
    Check the status of booking requests for a customer.
    
    Args:
        phone_number: Customer's phone number
        
    Returns:
        Dictionary with:
        - "success": bool
        - "bookings": List of booking dictionaries
        - "message": Status message
    """
    try:
        bookings = booking_manager.get_customer_bookings(phone_number)
        
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
                "status": booking.status
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
