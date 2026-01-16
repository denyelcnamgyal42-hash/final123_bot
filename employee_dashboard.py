"""
Employee dashboard API for approving/rejecting booking requests.
"""
from flask import Flask, request, jsonify, render_template_string
import logging
from booking_manager import BookingManager, BookingStatus
from excel_handler import ExcelHandler
import config
from datetime import datetime

logger = logging.getLogger(__name__)

# Import WhatsApp message sending function
try:
    from whatsapp_webhook import send_whatsapp_message
    WHATSAPP_AVAILABLE = True
except ImportError:
    logger.warning("WhatsApp webhook module not available. Notifications will not be sent.")
    WHATSAPP_AVAILABLE = False
    def send_whatsapp_message(phone_number: str, message: str, message_id: str = None):
        logger.warning(f"Would send WhatsApp message to {phone_number}: {message}")
        return None

app = Flask(__name__)
booking_manager = BookingManager()

# Initialize Excel handler
excel_handler = None
try:
    if config.GOOGLE_SHEET_ID:
        excel_handler = ExcelHandler(
            google_sheet_id=config.GOOGLE_SHEET_ID,
            google_credentials_path=config.GOOGLE_SHEETS_CREDENTIALS_PATH,
            sheet_name=config.HOTELS_SHEET
        )
    else:
        excel_handler = ExcelHandler(
            excel_path=config.EXCEL_PATH,
            sheet_name=config.HOTELS_SHEET
        )
except Exception as e:
    logger.error(f"Failed to initialize Excel handler: {e}")
    logger.warning("Excel handler not available. Booking approvals will not update the Excel sheet.")
    logger.warning("Please configure either GOOGLE_SHEET_ID or ensure EXCEL_PATH points to a valid file.")


def require_auth(f):
    """Decorator to require authentication token."""
    from functools import wraps
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_token = request.headers.get('Authorization') or request.args.get('token')
        
        if not auth_token:
            return jsonify({"error": "Authentication required"}), 401
        
        # Remove "Bearer " prefix if present
        if auth_token.startswith('Bearer '):
            auth_token = auth_token[7:]
        
        if auth_token != config.DASHBOARD_AUTH_TOKEN:
            return jsonify({"error": "Invalid authentication token"}), 403
        
        return f(*args, **kwargs)
    
    return decorated_function


@app.route("/dashboard", methods=["GET"])
@require_auth
def dashboard():
    """Simple HTML dashboard for viewing and managing bookings."""
    pending_bookings = booking_manager.get_pending_bookings()
    all_bookings = booking_manager.get_all_bookings()
    
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Hotel Booking Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }
            h1 { color: #333; }
            .booking-card { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; background: #fafafa; }
            .booking-card.pending { border-left: 4px solid #ff9800; }
            .booking-card.approved { border-left: 4px solid #4caf50; }
            .booking-card.rejected { border-left: 4px solid #f44336; }
            .booking-info { margin: 5px 0; }
            .actions { margin-top: 10px; }
            button { padding: 8px 16px; margin: 5px; cursor: pointer; border: none; border-radius: 4px; }
            .btn-approve { background: #4caf50; color: white; }
            .btn-reject { background: #f44336; color: white; }
            .status-badge { display: inline-block; padding: 4px 8px; border-radius: 3px; font-size: 12px; font-weight: bold; }
            .status-pending { background: #ff9800; color: white; }
            .status-approved { background: #4caf50; color: white; }
            .status-rejected { background: #f44336; color: white; }
            .tabs { display: flex; margin-bottom: 20px; }
            .tab { padding: 10px 20px; cursor: pointer; border-bottom: 2px solid transparent; }
            .tab.active { border-bottom: 2px solid #2196F3; color: #2196F3; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🏨 Hotel Booking Dashboard</h1>
            
            <div class="tabs">
                <div class="tab active" onclick="showTab('pending')">Pending ({{ pending_count }})</div>
                <div class="tab" onclick="showTab('all')">All Bookings</div>
            </div>
            
            <div id="pending-tab" class="tab-content active">
                <h2>Pending Booking Requests</h2>
                {% if pending_bookings %}
                    {% for booking in pending_bookings %}
                    <div class="booking-card pending">
                        <div class="booking-info"><strong>Booking ID:</strong> {{ booking.booking_id }}</div>
                        <div class="booking-info"><strong>Customer:</strong> {{ booking.customer_name }}</div>
                        <div class="booking-info"><strong>Phone:</strong> {{ booking.phone_number }}</div>
                        <div class="booking-info"><strong>Check-in:</strong> {{ booking.check_in_date }}</div>
                        <div class="booking-info"><strong>Check-out:</strong> {{ booking.check_out_date }}</div>
                        <div class="booking-info"><strong>Rooms:</strong> {{ booking.num_rooms }}</div>
                        <div class="booking-info"><strong>Guests:</strong> {{ booking.num_guests }}</div>
                        {% if booking.room_type_preference %}
                        <div class="booking-info"><strong>Room Type Preference:</strong> {{ booking.room_type_preference }}</div>
                        {% endif %}
                        <div class="booking-info"><strong>Created:</strong> {{ booking.created_at }}</div>
                        <div class="actions">
                            <button class="btn-approve" onclick="approveBooking('{{ booking.booking_id }}')">✓ Approve</button>
                            <button class="btn-reject" onclick="rejectBooking('{{ booking.booking_id }}')">✗ Reject</button>
                        </div>
                    </div>
                    {% endfor %}
                {% else %}
                    <p>No pending bookings.</p>
                {% endif %}
            </div>
            
            <div id="all-tab" class="tab-content">
                <h2>All Bookings</h2>
                {% if all_bookings %}
                    {% for booking in all_bookings %}
                    <div class="booking-card {{ booking.status }}">
                        <div class="booking-info"><strong>Booking ID:</strong> {{ booking.booking_id }}</div>
                        <div class="booking-info"><strong>Customer:</strong> {{ booking.customer_name }}</div>
                        <div class="booking-info"><strong>Phone:</strong> {{ booking.phone_number }}</div>
                        <div class="booking-info"><strong>Check-in:</strong> {{ booking.check_in_date }}</div>
                        <div class="booking-info"><strong>Check-out:</strong> {{ booking.check_out_date }}</div>
                        <div class="booking-info"><strong>Rooms:</strong> {{ booking.num_rooms }}</div>
                        <div class="booking-info"><strong>Guests:</strong> {{ booking.num_guests }}</div>
                        {% if booking.room_type_preference %}
                        <div class="booking-info"><strong>Room Type Preference:</strong> {{ booking.room_type_preference }}</div>
                        {% endif %}
                        <div class="booking-info">
                            <strong>Status:</strong> 
                            <span class="status-badge status-{{ booking.status }}">{{ booking.status.upper() }}</span>
                        </div>
                        {% if booking.approved_at %}
                        <div class="booking-info"><strong>Approved:</strong> {{ booking.approved_at }}</div>
                        {% endif %}
                        {% if booking.rejected_at %}
                        <div class="booking-info"><strong>Rejected:</strong> {{ booking.rejected_at }}</div>
                        {% if booking.rejection_reason %}
                        <div class="booking-info"><strong>Reason:</strong> {{ booking.rejection_reason }}</div>
                        {% endif %}
                        {% endif %}
                    </div>
                    {% endfor %}
                {% else %}
                    <p>No bookings found.</p>
                {% endif %}
            </div>
        </div>
        
        <script>
            function showTab(tabName) {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                
                if (tabName === 'pending') {
                    document.querySelector('.tab').classList.add('active');
                    document.getElementById('pending-tab').classList.add('active');
                } else {
                    document.querySelectorAll('.tab')[1].classList.add('active');
                    document.getElementById('all-tab').classList.add('active');
                }
            }
            
            function approveBooking(bookingId) {
                if (!confirm('Approve this booking? This will update the Excel sheet.')) return;
                
                fetch(`/api/approve/${bookingId}?token={{ token }}`, {
                    method: 'POST'
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        alert('Booking approved! Excel sheet updated.');
                        location.reload();
                    } else {
                        alert('Error: ' + data.message);
                    }
                })
                .catch(error => {
                    alert('Error: ' + error);
                });
            }
            
            function rejectBooking(bookingId) {
                const reason = prompt('Rejection reason (optional):');
                
                fetch(`/api/reject/${bookingId}?token={{ token }}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ reason: reason || '' })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        alert('Booking rejected.');
                        location.reload();
                    } else {
                        alert('Error: ' + data.message);
                    }
                })
                .catch(error => {
                    alert('Error: ' + error);
                });
            }
        </script>
    </body>
    </html>
    """
    
    return render_template_string(
        html_template,
        pending_bookings=pending_bookings,
        all_bookings=all_bookings,
        pending_count=len(pending_bookings),
        token=request.args.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    )


@app.route("/api/bookings", methods=["GET"])
@require_auth
def get_bookings():
    """Get all bookings (API endpoint)."""
    status = request.args.get('status')
    bookings = booking_manager.get_all_bookings(status=status)
    
    return jsonify({
        "success": True,
        "bookings": [b.to_dict() for b in bookings]
    })


@app.route("/api/bookings/pending", methods=["GET"])
@require_auth
def get_pending_bookings():
    """Get pending bookings (API endpoint)."""
    bookings = booking_manager.get_pending_bookings()
    
    return jsonify({
        "success": True,
        "bookings": [b.to_dict() for b in bookings]
    })


@app.route("/api/approve/<booking_id>", methods=["POST"])
@require_auth
def approve_booking(booking_id: str):
    """Approve a booking request and update Excel sheet."""
    try:
        booking = booking_manager.get_booking(booking_id)
        if not booking:
            return jsonify({"success": False, "message": "Booking not found"}), 404
        
        # Approve the booking
        success, message = booking_manager.approve_booking(booking_id)
        if not success:
            return jsonify({"success": False, "message": message}), 400
        
        # Update Excel sheet
        if excel_handler:
            excel_success, excel_message = excel_handler.update_booking(
                check_in=booking.check_in_date,
                check_out=booking.check_out_date,
                num_rooms=booking.num_rooms,
                num_guests=booking.num_guests,
                room_type_preference=booking.room_type_preference
            )
            
            if not excel_success:
                logger.error(f"Excel update failed for booking {booking_id}: {excel_message}")
                return jsonify({
                    "success": False,
                    "message": f"Booking approved but Excel update failed: {excel_message}"
                }), 500
            
            # Log approved booking to monthly sheet
            booking_dict = booking.to_dict()
            log_success, log_message = excel_handler.log_approved_booking(booking_dict)
            if not log_success:
                logger.warning(f"Failed to log booking to monthly sheet: {log_message}")
            else:
                logger.info(f"Booking logged to monthly sheet: {log_message}")
        
        # Send WhatsApp notification to customer
        if WHATSAPP_AVAILABLE:
            try:
                # Format dates nicely
                try:
                    check_in_date = datetime.strptime(booking.check_in_date, '%Y-%m-%d')
                    formatted_check_in = check_in_date.strftime('%B %d, %Y')  # e.g., "February 01, 2026"
                except:
                    formatted_check_in = booking.check_in_date
                
                try:
                    check_out_date = datetime.strptime(booking.check_out_date, '%Y-%m-%d')
                    formatted_check_out = check_out_date.strftime('%B %d, %Y')
                except:
                    formatted_check_out = booking.check_out_date
                
                message = (
                    f"Hello {booking.customer_name},\n\n"
                    f"Great news! Your booking request (ID: {booking.booking_id}) has been approved. "
                    f"We're looking forward to your stay on {formatted_check_in}.\n\n"
                    f"Booking Details:\n"
                    f"• Check-in: {formatted_check_in}\n"
                    f"• Check-out: {formatted_check_out}\n"
                    f"• Rooms: {booking.num_rooms}\n"
                    f"• Guests: {booking.num_guests}\n"
                    f"• Room Type: {booking.room_type_preference or 'Any available'}\n\n"
                    f"Thank you for choosing us! If you have any questions, please don't hesitate to contact us."
                )
                
                send_whatsapp_message(booking.phone_number, message)
                logger.info(f"✅ Approval notification sent to {booking.phone_number}")
            except Exception as e:
                logger.error(f"Failed to send approval notification: {e}")
        
        logger.info(f"Booking {booking_id} approved and Excel updated")
        
        return jsonify({
            "success": True,
            "message": "Booking approved and Excel sheet updated successfully",
            "booking": booking.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Error approving booking: {e}", exc_info=True)
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/reject/<booking_id>", methods=["POST"])
@require_auth
def reject_booking(booking_id: str):
    """Reject a booking request."""
    try:
        data = request.get_json() or {}
        reason = data.get("reason", "")
        
        success, message = booking_manager.reject_booking(booking_id, reason=reason)
        
        if not success:
            return jsonify({"success": False, "message": message}), 400
        
        booking = booking_manager.get_booking(booking_id)
        
        # Send WhatsApp notification to customer
        if WHATSAPP_AVAILABLE:
            try:
                notification_message = (
                    f"Hello {booking.customer_name},\n\n"
                    f"We're sorry to inform you that your booking request (ID: {booking.booking_id}) "
                    f"could not be confirmed at this time."
                )
                
                if reason:
                    notification_message += f"\n\nReason: {reason}"
                
                notification_message += (
                    f"\n\nWe apologize for any inconvenience. "
                    f"If you'd like to discuss alternative dates or have any questions, "
                    f"please feel free to contact us. We'd be happy to help you find a suitable option."
                )
                
                send_whatsapp_message(booking.phone_number, notification_message)
                logger.info(f"✅ Rejection notification sent to {booking.phone_number}")
            except Exception as e:
                logger.error(f"Failed to send rejection notification: {e}")
        
        return jsonify({
            "success": True,
            "message": "Booking rejected successfully",
            "booking": booking.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Error rejecting booking: {e}", exc_info=True)
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/bookings/<booking_id>", methods=["GET"])
@require_auth
def get_booking(booking_id: str):
    """Get a specific booking by ID."""
    booking = booking_manager.get_booking(booking_id)
    
    if not booking:
        return jsonify({"success": False, "message": "Booking not found"}), 404
    
    return jsonify({
        "success": True,
        "booking": booking.to_dict()
    })


if __name__ == "__main__":
    logger.info(f"Starting employee dashboard on port {config.DASHBOARD_PORT}")
    app.run(
        host="0.0.0.0",
        port=config.DASHBOARD_PORT,
        debug=config.DEBUG
    )
