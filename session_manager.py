"""Session management for conversation history and context."""
import json
import os
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field
import hashlib

import config
from state_store import GoogleSheetsKVStore, should_use_google_state

logger = logging.getLogger(__name__)

@dataclass
class Message:
    """Represents a single message in conversation history."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self):
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp
        }

@dataclass
class SessionContext:
    """Contextual information for the session."""
    pending_order: Optional[Dict] = None
    pending_booking: Optional[Dict] = None
    last_intent: Optional[str] = None  # "order", "booking", "inquiry", "support"
    current_product: Optional[str] = None
    current_hotel: Optional[str] = None
    cart: List[Dict] = field(default_factory=list)  # For multi-item purchases
    preferences: Dict[str, Any] = field(default_factory=dict)  # User preferences
    
    def to_dict(self):
        return {
            "pending_order": self.pending_order,
            "pending_booking": self.pending_booking,
            "last_intent": self.last_intent,
            "current_product": self.current_product,
            "current_hotel": self.current_hotel,
            "cart": self.cart,
            "preferences": self.preferences
        }

class Session:
    """Represents a user session."""
    def __init__(self, phone_number: str):
        self.phone_number = phone_number
        self.created_at = datetime.now()
        self.last_active = datetime.now()
        self.history: List[Message] = []
        self.context = SessionContext()
        self.session_id = hashlib.md5(f"{phone_number}{self.created_at.isoformat()}".encode()).hexdigest()[:8]
    
    def add_message(self, role: str, content: str):
        """Add a message to history."""
        self.history.append(Message(role=role, content=content))
        self.last_active = datetime.now()
        
        # Keep only last 10 messages to avoid excessive growth
        if len(self.history) > 10:
            self.history = self.history[-10:]
    
    def get_conversation_summary(self, max_messages: int = 5) -> str:
        """Get formatted conversation summary for agent context."""
        recent = self.history[-max_messages:] if self.history else []
        return "\n".join([f"{msg.role}: {msg.content}" for msg in recent])
    
    def update_context(self, **kwargs):
        """Update session context."""
        for key, value in kwargs.items():
            if hasattr(self.context, key):
                setattr(self.context, key, value)
        self.last_active = datetime.now()
    
    def clear_cart(self):
        """Clear the shopping cart."""
        self.context.cart = []
    
    def add_to_cart(self, product: Dict, quantity: int = 1):
        """Add product to cart."""
        # Check if product already in cart
        for item in self.context.cart:
            if item.get("product_name") == product.get("name"):
                item["quantity"] += quantity
                break
        else:
            self.context.cart.append({
                "product_name": product.get("name"),
                "quantity": quantity,
                "price": product.get("price"),
                "product_data": product
            })
    
    def to_dict(self) -> Dict:
        """Convert session to dictionary for serialization."""
        return {
            "phone_number": self.phone_number,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "history": [msg.to_dict() for msg in self.history],
            "context": self.context.to_dict(),
            "session_id": self.session_id
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Session':
        """Create session from dictionary."""
        session = cls(data["phone_number"])
        session.created_at = datetime.fromisoformat(data["created_at"])
        session.last_active = datetime.fromisoformat(data["last_active"])
        session.session_id = data.get("session_id", session.session_id)
        
        # Restore history
        session.history = [
            Message(role=msg["role"], content=msg["content"], timestamp=msg["timestamp"])
            for msg in data.get("history", [])
        ]
        
        # Restore context
        context_data = data.get("context", {})
        session.context = SessionContext(
            pending_order=context_data.get("pending_order"),
            pending_booking=context_data.get("pending_booking"),
            last_intent=context_data.get("last_intent"),
            current_product=context_data.get("current_product"),
            current_hotel=context_data.get("current_hotel"),
            cart=context_data.get("cart", []),
            preferences=context_data.get("preferences", {})
        )
        
        return session

class SessionManager:
    """Manages user sessions with persistence."""
    
    def __init__(
        self,
        session_file: str = "sessions.json",
        ttl_hours: int = 48,
        *,
        flush_interval_seconds: int = 15,
        flush_batch_size: int = 25,
    ):
        """
        Initialize session manager.
        
        Args:
            session_file: Path to session storage file
            ttl_hours: Session time-to-live in hours
            flush_interval_seconds: How often to flush dirty sessions to storage
            flush_batch_size: Flush immediately when this many sessions are dirty
        """
        self.session_file = session_file
        self.ttl_hours = ttl_hours
        self.sessions: Dict[str, Session] = {}
        self.lock = threading.RLock()  # Thread-safe operations

        self.flush_interval_seconds = flush_interval_seconds
        self.flush_batch_size = flush_batch_size
        self._dirty_sessions: set[str] = set()

        self._use_google_state = should_use_google_state()
        self._store: Optional[GoogleSheetsKVStore] = None
        if self._use_google_state:
            try:
                self._store = GoogleSheetsKVStore(
                    sheet_id=config.GOOGLE_SHEET_ID,
                    credentials_path=config.GOOGLE_SHEETS_CREDENTIALS_PATH,
                    worksheet_name=config.SESSIONS_SHEET,
                )
            except Exception as e:
                logger.warning(f"⚠️ Failed to init Google Sheets session store: {e}. Falling back to local JSON.")
                self._use_google_state = False
                self._store = None
        
        # Load existing sessions (only from local file; Sheets sessions are loaded on-demand)
        if not self._use_google_state:
            self._load_sessions()
        
        # Start cleanup scheduler
        self._start_cleanup_scheduler()

        # Start periodic flush thread (Sheets and local both benefit from batching)
        self._start_flush_scheduler()
    
    def _load_sessions(self):
        """Load sessions from storage file."""
        if os.path.exists(self.session_file):
            try:
                with open(self.session_file, 'r') as f:
                    data = json.load(f)
                
                with self.lock:
                    for phone, session_data in data.items():
                        try:
                            session = Session.from_dict(session_data)
                            # Check if session is still valid (not expired)
                            if self._is_session_valid(session):
                                self.sessions[phone] = session
                        except Exception as e:
                            logger.warning(f"Error loading session for {phone}: {e}")
                            continue
                
                logger.info(f"Loaded {len(self.sessions)} valid sessions from storage")
            except Exception as e:
                logger.error(f"Error loading session file: {e}. Starting with empty sessions.")
                self.sessions = {}
        else:
            self.sessions = {}
    
    def _save_sessions(self):
        """Save sessions to storage file."""
        if self._use_google_state:
            # When using Sheets, we persist via the flush scheduler / upsert calls.
            return
        with self.lock:
            data = {phone: session.to_dict() for phone, session in self.sessions.items()}
        
        try:
            # Write to temporary file first, then rename (atomic operation)
            temp_file = f"{self.session_file}.tmp"
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            
            os.replace(temp_file, self.session_file)
        except Exception as e:
            logger.error(f"Error saving sessions: {e}")

    def _load_session_from_store(self, phone_number: str) -> Optional[Session]:
        """Load a single session from Google Sheets KV store (if enabled)."""
        if not self._use_google_state or not self._store:
            return None
        try:
            raw = self._store.get_json(phone_number)
            if not raw:
                return None
            data = json.loads(raw)
            session = Session.from_dict(data)
            if not self._is_session_valid(session):
                # Best effort cleanup
                try:
                    self._store.delete(phone_number)
                except Exception:
                    pass
                return None
            return session
        except Exception as e:
            logger.error(f"Error loading session from Sheets for {phone_number}: {e}")
            return None

    def _flush_dirty_sessions(self) -> None:
        """Flush dirty sessions to persistent storage (Google Sheets or local JSON)."""
        # Snapshot dirty keys quickly
        with self.lock:
            if not self._dirty_sessions:
                return
            dirty = list(self._dirty_sessions)
            self._dirty_sessions.clear()

            # Build payload
            payload: Dict[str, str] = {}
            for phone in dirty:
                session = self.sessions.get(phone)
                if not session:
                    continue
                payload[phone] = json.dumps(session.to_dict(), default=str)

        if not payload:
            return

        if self._use_google_state and self._store:
            try:
                self._store.set_many_json(payload)
                return
            except Exception as e:
                # If flush fails, re-mark dirty for retry
                logger.warning(f"⚠️ Error flushing sessions to Sheets: {e}")
                with self.lock:
                    self._dirty_sessions.update(payload.keys())
                return

        # Fallback: local file persistence
        self._save_sessions()

    def _start_flush_scheduler(self):
        """Start background thread to periodically flush dirty sessions."""

        def flush_job():
            import time

            while True:
                time.sleep(self.flush_interval_seconds)
                try:
                    self._flush_dirty_sessions()
                except Exception:
                    # Never let the flush thread die
                    pass

        thread = threading.Thread(target=flush_job, daemon=True)
        thread.start()
    
    def _is_session_valid(self, session: Session) -> bool:
        """Check if session is still within TTL."""
        expiry_time = session.last_active + timedelta(hours=self.ttl_hours)
        return datetime.now() < expiry_time
    
    def _cleanup_expired_sessions(self):
        """Remove expired sessions."""
        with self.lock:
            expired_count = 0
            valid_sessions = {}
            
            for phone, session in self.sessions.items():
                if self._is_session_valid(session):
                    valid_sessions[phone] = session
                else:
                    expired_count += 1
            
            if expired_count > 0:
                self.sessions = valid_sessions
                if not self._use_google_state:
                    self._save_sessions()
                logger.info(f"Cleaned up {expired_count} expired sessions")
    
    def _start_cleanup_scheduler(self):
        """Start background thread for session cleanup."""
        def cleanup_job():
            import time
            while True:
                time.sleep(3600)  # Run every hour
                self._cleanup_expired_sessions()
        
        thread = threading.Thread(target=cleanup_job, daemon=True)
        thread.start()
    
    def get_session(self, phone_number: str) -> Session:
        """
        Get existing session or create new one.
        
        Args:
            phone_number: User's WhatsApp phone number
            
        Returns:
            Session object
        """
        with self.lock:
            if phone_number not in self.sessions:
                # Try to load from Sheets first (if enabled)
                session = self._load_session_from_store(phone_number) if self._use_google_state else None
                if session is None:
                    session = Session(phone_number)
                self.sessions[phone_number] = session
                logger.debug(f"Created new session for {phone_number}")
                # Mark dirty for persistence (batched)
                self._dirty_sessions.add(phone_number)
            else:
                session = self.sessions[phone_number]
                session.last_active = datetime.now()
            
            return session
    
    def update_session(self, phone_number: str, session: Session):
        """Update session in storage."""
        with self.lock:
            self.sessions[phone_number] = session
            session.last_active = datetime.now()
            self._dirty_sessions.add(phone_number)
            # Flush quickly if we have a lot of dirty sessions queued
            if len(self._dirty_sessions) >= self.flush_batch_size:
                # Flush outside lock to avoid long blocking
                pass

        if len(getattr(self, "_dirty_sessions", set())) >= self.flush_batch_size:
            self._flush_dirty_sessions()
    
    def add_message(self, phone_number: str, role: str, content: str):
        """Add message to session history and save."""
        session = self.get_session(phone_number)
        session.add_message(role, content)
        self.update_session(phone_number, session)
    
    def update_context(self, phone_number: str, **kwargs):
        """Update session context."""
        session = self.get_session(phone_number)
        session.update_context(**kwargs)
        self.update_session(phone_number, session)
    
    def clear_cart(self, phone_number: str):
        """Clear session's shopping cart."""
        session = self.get_session(phone_number)
        session.clear_cart()
        self.update_session(phone_number, session)
    
    def add_to_cart(self, phone_number: str, product: Dict, quantity: int = 1):
        """Add product to session's cart."""
        session = self.get_session(phone_number)
        session.add_to_cart(product, quantity)
        self.update_session(phone_number, session)
    
    def get_cart_total(self, phone_number: str) -> float:
        """Calculate total price of items in cart."""
        session = self.get_session(phone_number)
        total = 0.0
        for item in session.context.cart:
            try:
                price = float(item.get("price", 0))
                quantity = int(item.get("quantity", 1))
                total += price * quantity
            except (ValueError, TypeError):
                continue
        return total
    
    def get_session_stats(self) -> Dict:
        """Get session manager statistics."""
        with self.lock:
            now = datetime.now()
            active_24h = 0
            active_1h = 0
            
            for session in self.sessions.values():
                hours_since = (now - session.last_active).total_seconds() / 3600
                if hours_since < 1:
                    active_1h += 1
                if hours_since < 24:
                    active_24h += 1
            
            return {
                "total_sessions": len(self.sessions),
                "active_1h": active_1h,
                "active_24h": active_24h,
                "avg_messages_per_session": sum(len(s.history) for s in self.sessions.values()) / max(len(self.sessions), 1)
            }
    
    def delete_session(self, phone_number: str):
        """Delete a session."""
        with self.lock:
            if phone_number in self.sessions:
                del self.sessions[phone_number]
                self._dirty_sessions.discard(phone_number)
                if self._use_google_state and self._store:
                    try:
                        self._store.delete(phone_number)
                    except Exception:
                        pass
                else:
                    self._save_sessions()
                return True
            return False

# Global instance for easy import
session_manager = SessionManager()