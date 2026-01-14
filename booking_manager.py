"""
Booking request management system.
Stores booking requests and handles employee approval workflow.
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


class BookingStatus(Enum):
    """Booking request status."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


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
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict):
        return cls(**data)


class BookingManager:
    """Manages booking requests with persistence."""
    
    def __init__(self, storage_file: str = "bookings.json"):
        """
        Initialize booking manager.
        
        Args:
            storage_file: Path to JSON file for storing bookings
        """
        self.storage_file = storage_file
        self.lock = threading.RLock()
        self.bookings: Dict[str, BookingRequest] = {}
        self._load_bookings()
    
    def _load_bookings(self):
        """Load bookings from storage file."""
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
                
                logger.info(f"Loaded {len(self.bookings)} bookings from storage")
            except Exception as e:
                logger.error(f"Error loading bookings file: {e}")
                self.bookings = {}
        else:
            self.bookings = {}
    
    def _save_bookings(self):
        """Save bookings to storage file."""
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
        
        # Validate max guests per room type if room type preference is specified
        if room_type_preference:
            room_type_lower = room_type_preference.strip().lower()
            max_guests_per_room = None
            
            # Determine max guests based on room type
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
        
        self._save_bookings()
        logger.info(f"Created booking request: {booking_id}")
        
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
