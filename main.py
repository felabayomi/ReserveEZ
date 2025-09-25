import os, datetime as dt, io, json
from decimal import Decimal
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, send_file, flash
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import qrcode
import requests
import stripe

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "dev")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///easydesk.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# Environment configuration
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")
MERCURY_API_KEY = os.getenv("MERCURY_API_KEY", "")
MERCURY_WEBHOOK_SECRET = os.getenv("MERCURY_WEBHOOK_SECRET", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
PROMO_CODE = os.getenv("PROMO_CODE", "EASYWEEK").strip().upper()
USE_MERCURY = os.getenv("USE_MERCURY", "true").lower() == "true"
ALLOW_POS_CHECKOUT = os.getenv("ALLOW_POS_CHECKOUT", "true").lower() == "true"

# Stripe configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
YOUR_DOMAIN = os.getenv('REPLIT_DEV_DOMAIN') if os.getenv('REPLIT_DEPLOYMENT') != '' else os.getenv('REPLIT_DOMAINS', 'localhost:5000').split(',')[0]

# ---------------- Models ----------------
class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    hourly_rate_cents = db.Column(db.Integer, nullable=False)
    day_rate_cents = db.Column(db.Integer, nullable=False, default=0)
    week_rate_cents = db.Column(db.Integer, nullable=False, default=0)
    month_rate_cents = db.Column(db.Integer, nullable=False, default=0)
    capacity = db.Column(db.Integer, default=10)
    opening_hours = db.Column(db.Text)  # JSON string
    active = db.Column(db.Boolean, default=True)

    def get_opening_hours(self):
        if self.opening_hours:
            try:
                return json.loads(self.opening_hours)
            except:
                pass
        # Default hours: Mon-Fri 9-6, Sat 10-2, Sun closed
        return {
            "mon": [["09:00", "18:00"]],
            "tue": [["09:00", "18:00"]],
            "wed": [["09:00", "18:00"]],
            "thu": [["09:00", "18:00"]],
            "fri": [["09:00", "18:00"]],
            "sat": [["10:00", "14:00"]],
            "sun": []
        }

    def set_opening_hours(self, hours_dict):
        self.opening_hours = json.dumps(hours_dict)

class Pass(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), nullable=False, index=True)
    pass_type = db.Column(db.String(20), nullable=False)  # day, week, month
    purchase_dt = db.Column(db.DateTime, default=dt.datetime.utcnow)
    valid_from = db.Column(db.DateTime, nullable=False)
    valid_to = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="active")  # active, expired, refunded

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("booking.id"), nullable=True)
    pass_id = db.Column(db.Integer, db.ForeignKey("pass.id"), nullable=True)
    provider = db.Column(db.String(20), nullable=False)  # mercury, chase, manual
    intent_id = db.Column(db.String(120))
    status = db.Column(db.String(20), default="created")  # created, pending, paid, failed, refunded
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(3), default="usd")
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), nullable=False, index=True)
    name = db.Column(db.String(120))
    resource_id = db.Column(db.Integer, db.ForeignKey("resource.id"), nullable=False)
    plan_type = db.Column(db.String(10), nullable=False, default="hour")  # hour, day, week, month
    seats = db.Column(db.Integer, default=1)
    start_dt = db.Column(db.DateTime, nullable=False)
    end_dt = db.Column(db.DateTime, nullable=False)
    hours = db.Column(db.Float, nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="reserved")  # reserved, paid, cancelled, checked_in, free
    promo_applied = db.Column(db.String(40))
    pass_id = db.Column(db.Integer, db.ForeignKey("pass.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    resource = db.relationship("Resource")

# ---------------- Database Setup ----------------
def initialize_database():
    """Initialize database with proper error handling"""
    with app.app_context():
        # Force drop and recreate all tables for schema changes
        db.drop_all()
        db.create_all()
        
        # Check if we need to seed data
        if Resource.query.count() == 0:
            default_hours = {
                "mon": [["09:00", "18:00"]],
                "tue": [["09:00", "18:00"]],
                "wed": [["09:00", "18:00"]],
                "thu": [["09:00", "18:00"]],
                "fri": [["09:00", "18:00"]],
                "sat": [["10:00", "14:00"]],
                "sun": []
            }
            resources = []
            # Hot Desk: $5 hourly, $15 day, $60 week, $150 month, capacity 2
            hot_desk = Resource()
            hot_desk.name = "Hot Desk"
            hot_desk.hourly_rate_cents = 500
            hot_desk.day_rate_cents = 1500
            hot_desk.week_rate_cents = 6000
            hot_desk.month_rate_cents = 15000
            hot_desk.capacity = 2
            hot_desk.opening_hours = json.dumps(default_hours)
            resources.append(hot_desk)
        
            # Quiet Desk: $17 hourly, $30 day, $110 week, $275 month, capacity 1
            quiet_desk = Resource()
            quiet_desk.name = "Quiet Desk"
            quiet_desk.hourly_rate_cents = 1700
            quiet_desk.day_rate_cents = 3000
            quiet_desk.week_rate_cents = 11000
            quiet_desk.month_rate_cents = 27500
            quiet_desk.capacity = 1
            quiet_desk.opening_hours = json.dumps(default_hours)
            resources.append(quiet_desk)
        
            # Meeting Lounge: $15 hourly, $60 day, $200 week, $500 month, capacity 4
            meeting_table = Resource()
            meeting_table.name = "Meeting Lounge"
            meeting_table.hourly_rate_cents = 1500
            meeting_table.day_rate_cents = 6000
            meeting_table.week_rate_cents = 20000
            meeting_table.month_rate_cents = 50000
            meeting_table.capacity = 4
            meeting_table.opening_hours = json.dumps(default_hours)
            resources.append(meeting_table)
        
            # Whole Room: $50 hourly, $175 day, $600 week, $1500 month, capacity 1
            whole_room = Resource()
            whole_room.name = "Whole Room"
            whole_room.hourly_rate_cents = 5000
            whole_room.day_rate_cents = 17500
            whole_room.week_rate_cents = 60000
            whole_room.month_rate_cents = 150000
            whole_room.capacity = 1
            whole_room.opening_hours = json.dumps(default_hours)
            resources.append(whole_room)
            db.session.add_all(resources)
            db.session.commit()

# Initialize database on startup
initialize_database()

# ---------------- Helper Functions ----------------
def parse_dt(date_str, time_str):
    y, m, d = map(int, date_str.split("-"))
    hh, mm = map(int, time_str.split(":"))
    return dt.datetime(y, m, d, hh, mm)

def as_money(cents):
    return f"${Decimal(cents) / Decimal(100):.2f}"

def user_has_used_promo(email: str, code: str) -> bool:
    return Booking.query.filter(
        Booking.email == email,
        Booking.promo_applied == code,
        Booking.status.in_(["paid", "free", "checked_in"])
    ).count() > 0

def is_in_hours(resource, start_dt, end_dt):
    """Check if booking time falls within resource opening hours"""
    hours = resource.get_opening_hours()
    weekday = start_dt.strftime("%a").lower()
    
    if weekday not in hours or not hours[weekday]:
        return False
    
    start_time = start_dt.strftime("%H:%M")
    end_time = end_dt.strftime("%H:%M")
    
    for window in hours[weekday]:
        if len(window) == 2:
            open_time, close_time = window
            if start_time >= open_time and end_time <= close_time:
                return True
    return False

def seats_left(resource_id, start_dt, end_dt):
    """Calculate available seats for a resource during a time window"""
    resource = Resource.query.get(resource_id)
    if not resource:
        return 0
    
    # Sum up actual seats booked (not just count of bookings)
    used_seats = db.session.query(db.func.coalesce(db.func.sum(Booking.seats), 0)).filter(
        Booking.resource_id == resource_id,
        Booking.status.in_(["reserved", "paid", "checked_in", "free"]),
        Booking.start_dt < end_dt,
        Booking.end_dt > start_dt
    ).scalar() or 0
    
    return max(0, resource.capacity - used_seats)

def get_hourly_availability(resource_id, date):
    """Get detailed hourly availability for a resource on a specific date"""
    resource = Resource.query.get(resource_id)
    if not resource:
        return {}
    
    # Get opening hours for this date
    hours_data = resource.get_opening_hours()
    weekday = date.strftime("%a").lower()
    
    if weekday not in hours_data or not hours_data[weekday]:
        return {}  # Closed on this day
    
    availability = {}
    
    # Check each hour of the day
    for window in hours_data[weekday]:
        if len(window) == 2:
            open_time, close_time = window
            current_hour = dt.datetime.strptime(f"{date.strftime('%Y-%m-%d')} {open_time}", "%Y-%m-%d %H:%M")
            close_hour = dt.datetime.strptime(f"{date.strftime('%Y-%m-%d')} {close_time}", "%Y-%m-%d %H:%M")
            
            while current_hour < close_hour:
                hour_end = current_hour + dt.timedelta(hours=1)
                hour_key = current_hour.strftime("%H:%M")
                
                # Calculate seats available for this hour
                available_seats = seats_left(resource_id, current_hour, hour_end)
                availability[hour_key] = available_seats
                
                current_hour = hour_end
    
    return availability

def check_overflow_eligibility():
    """Check if Meeting Lounge should be available as overflow at $3/hr"""
    # Get Hot Desk and Quiet Desk IDs
    hot_desk = Resource.query.filter_by(name="Hot Desk").first()
    quiet_desk = Resource.query.filter_by(name="Quiet Desk").first()
    
    if not hot_desk or not quiet_desk:
        return False
    
    return hot_desk.id, quiet_desk.id

def get_resource_availability_for_date(date):
    """Get comprehensive availability for all resources on a specific date"""
    resources = Resource.query.filter_by(active=True).all()
    availability = {}
    
    hot_desk_id, quiet_desk_id = None, None
    meeting_lounge_id = None
    
    for resource in resources:
        resource_avail = {
            'resource': resource,
            'hourly_availability': get_hourly_availability(resource.id, date),
            'has_day_pass_booking': has_day_or_longer_pass_booking(resource.id, date),
            'available_seats': resource.capacity,
            'overflow_eligible': False
        }
        
        # Track specific resource IDs for overflow logic
        if resource.name == "Hot Desk":
            hot_desk_id = resource.id
        elif resource.name == "Quiet Desk":
            quiet_desk_id = resource.id
        elif resource.name == "Meeting Lounge":
            meeting_lounge_id = resource.id
        
        availability[resource.id] = resource_avail
    
    # Check overflow eligibility for Meeting Lounge
    if hot_desk_id and quiet_desk_id and meeting_lounge_id:
        hot_desk_full = is_resource_fully_booked(hot_desk_id, date)
        quiet_desk_full = is_resource_fully_booked(quiet_desk_id, date)
        
        if hot_desk_full and quiet_desk_full:
            availability[meeting_lounge_id]['overflow_eligible'] = True
            availability[meeting_lounge_id]['overflow_rate'] = 300  # $3/hr in cents
    
    return availability

def has_day_or_longer_pass_booking(resource_id, date):
    """Check if resource has any day/week/month pass bookings for the date"""
    start_of_day = dt.datetime.combine(date, dt.time.min)
    end_of_day = dt.datetime.combine(date, dt.time.max)
    
    pass_bookings = Booking.query.filter(
        Booking.resource_id == resource_id,
        Booking.plan_type.in_(["day", "week", "month"]),
        Booking.status.in_(["reserved", "paid", "checked_in", "free"]),
        Booking.start_dt <= end_of_day,
        Booking.end_dt >= start_of_day
    ).first()
    
    return pass_bookings is not None

def is_resource_fully_booked(resource_id, date):
    """Check if a resource is completely unavailable for the entire day"""
    resource = Resource.query.get(resource_id)
    if not resource:
        return True
    
    # If there's a day/week/month pass that uses all capacity, it's fully booked
    if has_day_or_longer_pass_booking(resource_id, date):
        return True
    
    # Check if all hourly slots are taken
    hourly_avail = get_hourly_availability(resource_id, date)
    
    if not hourly_avail:
        return True  # No operating hours = fully booked
    
    # Resource is fully booked if ALL hours have 0 available seats
    for hour, available_seats in hourly_avail.items():
        if available_seats > 0:
            return False  # Found at least one hour with available seats
    
    return True  # All hours are fully booked

def active_pass(email, at_dt):
    """Check for active pass for user at given datetime"""
    return Pass.query.filter(
        Pass.email == email,
        Pass.status == "active",
        Pass.valid_from <= at_dt,
        Pass.valid_to >= at_dt
    ).first()

def day_bookings(date_str):
    """Get bookings for a specific date grouped by resource"""
    y, m, d = map(int, date_str.split("-"))
    start = dt.datetime(y, m, d, 0, 0)
    end = start + dt.timedelta(days=1)
    
    bookings = Booking.query.filter(
        Booking.start_dt < end,
        Booking.end_dt > start,
        Booking.status.in_(["reserved", "paid", "checked_in", "free"])
    ).order_by(Booking.resource_id, Booking.start_dt).all()
    
    grouped = {}
    for b in bookings:
        grouped.setdefault(b.resource_id, []).append(b)
    return grouped

def validate_booking_availability(resource_id, plan_type, seats, start_dt, end_dt=None, hours=None):
    """Validate if a booking can be made with comprehensive seat + hour availability checking"""
    resource = Resource.query.get(resource_id)
    if not resource or not resource.active:
        return False, "Resource not available"
    
    # Calculate end_dt if not provided
    if not end_dt:
        end_dt = calculate_plan_duration(resource, plan_type, start_dt, hours)
    
    # Check if requested seats exceed capacity
    if seats > resource.capacity:
        return False, f"Requested {seats} seats exceeds capacity of {resource.capacity}"
    
    # Check opening hours for the entire duration
    if not is_booking_within_hours(resource, start_dt, end_dt):
        return False, "Booking time is outside opening hours"
    
    # New logic: Different validation for different plan types
    if plan_type == "hour":
        # For hourly bookings, check hour-by-hour availability
        return validate_hourly_booking(resource_id, seats, start_dt, end_dt)
    else:
        # For day/week/month passes, check if there are conflicts with existing passes
        return validate_pass_booking(resource_id, seats, start_dt, end_dt, plan_type)

def validate_hourly_booking(resource_id, seats, start_dt, end_dt):
    """Validate hourly booking with detailed hour-by-hour checking"""
    current_hour = start_dt
    
    while current_hour < end_dt:
        hour_end = current_hour + dt.timedelta(hours=1)
        available_seats = seats_left(resource_id, current_hour, hour_end)
        
        if available_seats < seats:
            time_str = current_hour.strftime("%H:%M")
            return False, f"Only {available_seats} seats available at {time_str}, but {seats} requested"
        
        current_hour = hour_end
    
    return True, "Available"

def validate_pass_booking(resource_id, seats, start_dt, end_dt, plan_type):
    """Validate day/week/month pass booking against existing bookings"""
    # Check if there are conflicting day/week/month passes
    conflicting_passes = Booking.query.filter(
        Booking.resource_id == resource_id,
        Booking.plan_type.in_(["day", "week", "month"]),
        Booking.status.in_(["reserved", "paid", "checked_in", "free"]),
        Booking.start_dt < end_dt,
        Booking.end_dt > start_dt
    ).all()
    
    # Calculate total seats used by existing passes
    pass_seats_used = sum(booking.seats for booking in conflicting_passes)
    
    # Check if hourly bookings would conflict
    hourly_conflicts = check_hourly_conflicts_with_pass(resource_id, seats, start_dt, end_dt)
    
    resource = Resource.query.get(resource_id)
    if not resource:
        return False, "Resource not found"
    
    total_needed = pass_seats_used + seats
    
    if total_needed > resource.capacity:
        return False, f"Pass booking conflicts with existing reservations. Only {resource.capacity - pass_seats_used} seats available."
    
    if hourly_conflicts:
        return False, "Pass booking conflicts with existing hourly bookings. Please choose different dates."
    
    return True, "Available"

def check_hourly_conflicts_with_pass(resource_id, pass_seats, start_dt, end_dt):
    """Check if a pass booking would conflict with existing hourly bookings"""
    # Get all hourly bookings that overlap with the pass period
    hourly_bookings = Booking.query.filter(
        Booking.resource_id == resource_id,
        Booking.plan_type == "hour",
        Booking.status.in_(["reserved", "paid", "checked_in", "free"]),
        Booking.start_dt < end_dt,
        Booking.end_dt > start_dt
    ).all()
    
    resource = Resource.query.get(resource_id)
    if not resource:
        return True  # If resource not found, consider it a conflict to be safe
    
    remaining_capacity = resource.capacity - pass_seats
    
    # Group hourly bookings by time slots
    hourly_usage = {}
    for booking in hourly_bookings:
        current = booking.start_dt
        while current < booking.end_dt:
            hour_key = current.strftime("%Y-%m-%d %H:%M")
            hourly_usage[hour_key] = hourly_usage.get(hour_key, 0) + booking.seats
            current += dt.timedelta(hours=1)
    
    # Check if any hour exceeds available capacity after pass booking
    for hour_key, seats_used in hourly_usage.items():
        if seats_used > remaining_capacity:
            return True  # Conflict found
    
    return False  # No conflicts

def is_booking_within_hours(resource, start_dt, end_dt):
    """Check if entire booking duration falls within opening hours"""
    hours_data = resource.get_opening_hours()
    
    # Check each day in the booking range
    current = start_dt.date()
    end_date = end_dt.date()
    
    while current <= end_date:
        day_key = current.strftime("%a").lower()
        
        if day_key not in hours_data or not hours_data[day_key]:
            # Closed on this day
            if current == start_dt.date() or current == end_dt.date():
                # If booking starts or ends on a closed day, invalid
                return False
            current += dt.timedelta(days=1)
            continue
        
        # Get the time range for this specific day
        if current == start_dt.date():
            check_start = start_dt.time()
        else:
            # For multi-day bookings, assume it starts at opening time
            check_start = dt.datetime.strptime(hours_data[day_key][0][0], "%H:%M").time()
        
        if current == end_dt.date():
            check_end = end_dt.time()
        else:
            # For multi-day bookings, assume it ends at closing time
            check_end = dt.datetime.strptime(hours_data[day_key][0][1], "%H:%M").time()
        
        # Check if this day's portion falls within opening hours
        day_valid = False
        for window in hours_data[day_key]:
            if len(window) == 2:
                open_time = dt.datetime.strptime(window[0], "%H:%M").time()
                close_time = dt.datetime.strptime(window[1], "%H:%M").time()
                if check_start >= open_time and check_end <= close_time:
                    day_valid = True
                    break
        
        if not day_valid:
            return False
        
        current += dt.timedelta(days=1)
    
    return True

# ---------------- Payment Functions ----------------
def create_mercury_invoice(amount_cents, email, memo, metadata=None):
    """Create Mercury payment invoice/link"""
    if not MERCURY_API_KEY:
        return {"error": "Mercury API key not configured"}
    
    # Mercury API implementation stub - replace with actual API calls
    # This is a placeholder that would make actual HTTP requests to Mercury
    invoice_data = {
        "amount": amount_cents,
        "email": email,
        "memo": memo,
        "metadata": metadata or {}
    }
    
    # For now, return a mock response
    return {
        "intent_id": f"mercury_{dt.datetime.utcnow().timestamp()}",
        "pay_url": f"{BASE_URL}/mock-mercury-checkout"
    }

def verify_mercury_webhook(request):
    """Verify Mercury webhook signature and return event data"""
    # Implementation stub for Mercury webhook verification
    return {"data": {"id": "test", "status": "paid"}}

def mark_payment_paid(provider, intent_id):
    """Mark payment as paid and update related booking/pass"""
    payment = Payment.query.filter_by(provider=provider, intent_id=intent_id).first()
    if not payment:
        return False
    
    payment.status = "paid"
    
    if payment.booking_id:
        booking = Booking.query.get(payment.booking_id)
        if booking:
            booking.status = "paid"
    
    if payment.pass_id:
        pass_obj = Pass.query.get(payment.pass_id)
        if pass_obj:
            pass_obj.status = "active"
    
    db.session.commit()
    return True

def calculate_pass_validity(pass_type, purchase_dt=None):
    """Calculate valid_from and valid_to for a pass"""
    if not purchase_dt:
        purchase_dt = dt.datetime.utcnow()
    
    if pass_type == "day":
        valid_from = purchase_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        valid_to = valid_from.replace(hour=23, minute=59, second=59)
    elif pass_type == "week":
        # Week pass: 7 days from purchase
        valid_from = purchase_dt
        valid_to = valid_from + dt.timedelta(days=7)
    elif pass_type == "month":
        # Month pass: 30 days from purchase
        valid_from = purchase_dt
        valid_to = valid_from + dt.timedelta(days=30)
    else:
        raise ValueError(f"Invalid pass type: {pass_type}")
    
    return valid_from, valid_to

def calculate_plan_price(resource, plan_type, seats=1, hours=1):
    """Calculate price for a booking plan"""
    if plan_type == "hour":
        return resource.hourly_rate_cents * hours * seats
    elif plan_type == "day":
        return resource.day_rate_cents * seats
    elif plan_type == "week":
        return resource.week_rate_cents * seats
    elif plan_type == "month":
        return resource.month_rate_cents * seats
    else:
        raise ValueError(f"Invalid plan type: {plan_type}")

def calculate_plan_duration(resource, plan_type, start_dt, hours=None):
    """Calculate end datetime for a booking plan"""
    if plan_type == "hour":
        if not hours:
            raise ValueError("Hours required for hourly booking")
        return start_dt + dt.timedelta(hours=hours)
    elif plan_type == "day":
        # Book for the entire day within opening hours
        hours_data = resource.get_opening_hours()
        day_key = start_dt.strftime("%a").lower()
        if day_key in hours_data and hours_data[day_key]:
            # Use first time window of the day
            open_time, close_time = hours_data[day_key][0]
            start_time = dt.datetime.strptime(open_time, "%H:%M").time()
            end_time = dt.datetime.strptime(close_time, "%H:%M").time()
            start_dt = start_dt.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)
            end_dt = start_dt.replace(hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0)
            return end_dt
        else:
            # Default to 9-17 if no hours configured
            start_dt = start_dt.replace(hour=9, minute=0, second=0, microsecond=0)
            return start_dt.replace(hour=17, minute=0, second=0, microsecond=0)
    elif plan_type == "week":
        return start_dt + dt.timedelta(days=7)
    elif plan_type == "month":
        return start_dt + dt.timedelta(days=30)
    else:
        raise ValueError(f"Invalid plan type: {plan_type}")

# ---------------- Routes ----------------
@app.get("/")
def index():
    resources = Resource.query.filter_by(active=True).all()
    return render_template("index.html", resources=resources)

@app.get("/book")
def book_page():
    resources = Resource.query.filter_by(active=True).all()
    date = request.args.get("date") or dt.date.today().isoformat()
    pre_selected_plan = request.args.get("plan")  # Get pre-selected plan from URL
    grouped = day_bookings(date)
    
    # Calculate capacity for each resource for the selected date
    capacity_info = {}
    for r in resources:
        # For simplicity, show capacity for 9 AM - 5 PM window
        start_dt = parse_dt(date, "09:00")
        end_dt = parse_dt(date, "17:00")
        available = seats_left(r.id, start_dt, end_dt)
        capacity_info[r.id] = {"available": available, "total": r.capacity}
    
    return render_template("book.html", 
                         resources=resources, 
                         promo=PROMO_CODE, 
                         date=date, 
                         grouped=grouped,
                         capacity_info=capacity_info,
                         as_money=as_money,
                         use_mercury=USE_MERCURY,
                         allow_pos=ALLOW_POS_CHECKOUT,
                         pre_selected_plan=pre_selected_plan)

@app.post("/book")
def create_booking():
    """Handle new plan-based booking system"""
    email = request.form["email"].strip().lower()
    name = request.form["name"]
    resource_id = int(request.form["resource_id"])
    plan_type = request.form["plan_type"]
    seats = int(request.form.get("seats", 1))
    code = request.form.get("promo", "").strip().upper()
    payment_method = request.form.get("payment_method", "online")
    
    return handle_plan_booking(email, name, resource_id, plan_type, seats, code, payment_method)

def handle_plan_booking(email, name, resource_id, plan_type, seats, code, payment_method):
    """Handle plan-based booking (hour/day/week/month)"""
    resource = Resource.query.get_or_404(resource_id)
    
    # Parse plan-specific date/time fields
    start_dt, end_dt, hours = parse_plan_dates(plan_type, resource)
    
    # Validate availability
    is_valid, error_msg = validate_booking_availability(
        resource_id, plan_type, seats, start_dt, end_dt, hours
    )
    
    if not is_valid:
        flash(f"Booking unavailable: {error_msg}", "warning")
        return redirect(url_for("book_page"))
    
    # Calculate pricing
    amount_cents = calculate_plan_price(resource, plan_type, seats, int(hours) if plan_type == "hour" else 1)
    
    # Check for active pass (for future pass integration)
    user_pass = active_pass(email, start_dt)
    if user_pass:
        amount_cents = 0
        status = "free"
    else:
        # Check promo code
        apply_free = (code == PROMO_CODE) and not user_has_used_promo(email, PROMO_CODE)
        if apply_free:
            amount_cents = 0
            status = "free"
        else:
            status = "reserved"
    
    # Create booking
    booking = Booking()
    booking.email = email
    booking.name = name
    booking.resource_id = resource_id
    booking.plan_type = plan_type
    booking.seats = seats
    booking.start_dt = start_dt
    booking.end_dt = end_dt
    booking.hours = hours if plan_type == "hour" else calculate_hours_from_range(start_dt, end_dt, resource)
    booking.amount_cents = amount_cents
    booking.status = status
    booking.promo_applied = PROMO_CODE if (code == PROMO_CODE and amount_cents == 0) else None
    
    db.session.add(booking)
    db.session.commit()
    
    # Handle free bookings
    if amount_cents == 0:
        return redirect(url_for("success_free", bid=booking.id))
    
    # Handle payments
    if payment_method == "pos" and ALLOW_POS_CHECKOUT:
        # Chase POS flow
        payment = Payment()
        payment.booking_id = booking.id
        payment.provider = "chase"
        payment.status = "pending"
        payment.amount_cents = amount_cents
        db.session.add(payment)
        db.session.commit()
        return redirect(url_for("success_pos", bid=booking.id))
    else:
        # Mercury online payment
        if USE_MERCURY and amount_cents > 0:
            mercury_result = create_mercury_invoice(
                amount_cents, email,
                f"{resource.name} ({plan_type} - {seats} seats)",
                {"type": "booking", "booking_id": booking.id}
            )
            
            if "error" in mercury_result:
                flash("Payment system unavailable. Please try again.", "error")
                return redirect(url_for("book_page"))
            
            payment = Payment()
            payment.booking_id = booking.id
            payment.provider = "mercury"
            payment.intent_id = mercury_result["intent_id"]
            payment.status = "created"
            payment.amount_cents = amount_cents
            db.session.add(payment)
            db.session.commit()
            
            return redirect(mercury_result["pay_url"])
        else:
            # No payment needed
            booking.status = "confirmed"
            db.session.commit()
            return redirect(url_for("success_free", bid=booking.id))

def parse_plan_dates(plan_type, resource):
    """Parse form data to get start_dt, end_dt, and hours for different plan types"""
    if plan_type == "hour":
        # Hourly booking
        booking_date = request.form.get("booking_date") or request.form.get("date")
        start_time = request.form["start_time"]
        end_time = request.form["end_time"]
        
        start_dt = parse_dt(booking_date, start_time)
        end_dt = parse_dt(booking_date, end_time)
        
        # Calculate hours
        hours = (end_dt - start_dt).total_seconds() / 3600
        
        return start_dt, end_dt, hours
    
    elif plan_type == "day":
        # Day pass booking
        booking_date = request.form.get("booking_date") or request.form.get("date")
        
        # Get opening hours for this day
        hours_data = resource.get_opening_hours()
        parsed_date = dt.datetime.strptime(booking_date or dt.date.today().isoformat(), "%Y-%m-%d")
        day_key = parsed_date.strftime("%a").lower()
        
        if day_key in hours_data and hours_data[day_key]:
            open_time, close_time = hours_data[day_key][0]
            start_dt = parse_dt(booking_date, open_time)
            end_dt = parse_dt(booking_date, close_time)
        else:
            # Default hours if not configured
            start_dt = parse_dt(booking_date, "09:00")
            end_dt = parse_dt(booking_date, "17:00")
        
        return start_dt, end_dt, 8.0  # Standard 8-hour day
    
    elif plan_type in ["week", "month"]:
        # Week/month pass booking
        start_date = request.form.get("start_date") or request.form.get("date")
        
        start_dt = parse_dt(start_date, "09:00")  # Start at 9 AM
        
        if plan_type == "week":
            end_dt = start_dt + dt.timedelta(days=7)
        else:  # month
            end_dt = start_dt + dt.timedelta(days=30)
        
        # Replace end time with closing time
        end_dt = end_dt.replace(hour=17, minute=0, second=0, microsecond=0)
        
        return start_dt, end_dt, 168.0 if plan_type == "week" else 720.0  # Approximate hours
    
    else:
        raise ValueError(f"Invalid plan type: {plan_type}")

def calculate_hours_from_range(start_dt, end_dt, resource):
    """Calculate effective booking hours within opening hours"""
    hours_data = resource.get_opening_hours()
    total_hours = 0
    
    current = start_dt.date()
    end_date = end_dt.date()
    
    while current <= end_date:
        day_key = current.strftime("%a").lower()
        
        if day_key in hours_data and hours_data[day_key]:
            for window in hours_data[day_key]:
                if len(window) == 2:
                    open_time = dt.datetime.strptime(window[0], "%H:%M").time()
                    close_time = dt.datetime.strptime(window[1], "%H:%M").time()
                    
                    day_open = dt.datetime.combine(current, open_time)
                    day_close = dt.datetime.combine(current, close_time)
                    
                    # Calculate overlap with booking window
                    effective_start = max(start_dt, day_open)
                    effective_end = min(end_dt, day_close)
                    
                    if effective_start < effective_end:
                        total_hours += (effective_end - effective_start).total_seconds() / 3600
        
        current += dt.timedelta(days=1)
    
    return total_hours

def handle_pass_purchase(email, name):
    """Handle pass purchase"""
    pass_type = request.form["pass_type"]
    payment_method = request.form.get("payment_method", "online")
    
    # Pass pricing
    pass_prices = {"day": 1000, "week": 3000, "month": 12000}  # cents
    amount_cents = pass_prices.get(pass_type, 0)
    
    if amount_cents == 0:
        flash("Invalid pass type.", "error")
        return redirect(url_for("book_page"))
    
    # Create pass
    purchase_dt = dt.datetime.utcnow()
    valid_from, valid_to = calculate_pass_validity(pass_type, purchase_dt)
    
    pass_obj = Pass()
    pass_obj.email = email
    pass_obj.pass_type = pass_type
    pass_obj.purchase_dt = purchase_dt
    pass_obj.valid_from = valid_from
    pass_obj.valid_to = valid_to
    pass_obj.status = "pending"
    db.session.add(pass_obj)
    db.session.commit()
    
    if payment_method == "pos" and ALLOW_POS_CHECKOUT:
        # Chase POS flow
        payment = Payment()
        payment.pass_id = pass_obj.id
        payment.provider = "chase"
        payment.status = "pending"
        payment.amount_cents = amount_cents
        db.session.add(payment)
        db.session.commit()
        return redirect(url_for("success_pos_pass", pid=pass_obj.id))
    else:
        # Mercury online payment
        if USE_MERCURY:
            mercury_result = create_mercury_invoice(
                amount_cents, email,
                f"{pass_type.title()} Pass",
                {"type": "pass", "pass_id": pass_obj.id}
            )
            
            if "error" in mercury_result:
                flash("Payment system unavailable. Please try again.", "error")
                return redirect(url_for("book_page"))
            
            payment = Payment()
            payment.pass_id = pass_obj.id
            payment.provider = "mercury"
            payment.intent_id = mercury_result["intent_id"]
            payment.status = "created"
            payment.amount_cents = amount_cents
            db.session.add(payment)
            db.session.commit()
            
            return redirect(mercury_result["pay_url"])
    
    return redirect(url_for("book_page"))

@app.get("/success")
def success():
    # Mercury payment success (would be called by Mercury redirect)
    payment_id = request.args.get("payment_id")
    # In real implementation, verify payment with Mercury API
    return render_template("success.html", message="Payment completed successfully!")

@app.get("/success-free")
def success_free():
    bid_str = request.args.get("bid")
    if not bid_str:
        abort(400)
    bid = int(bid_str)
    booking = Booking.query.get_or_404(bid)
    return render_template("success.html", booking=booking, as_money=as_money)

@app.get("/success-pos")
def success_pos():
    bid_str = request.args.get("bid")
    if not bid_str:
        abort(400)
    bid = int(bid_str)
    booking = Booking.query.get_or_404(bid)
    return render_template("success_pos.html", booking=booking, as_money=as_money)

@app.get("/success-pos-pass")
def success_pos_pass():
    pid_str = request.args.get("pid")
    if not pid_str:
        abort(400)
    pid = int(pid_str)
    pass_obj = Pass.query.get_or_404(pid)
    return render_template("success_pos_pass.html", pass_obj=pass_obj)

@app.get("/cancel")
def cancel():
    bid = request.args.get("bid")
    return render_template("cancel.html", bid=bid)

@app.get("/api/availability/<date>")
def api_availability(date):
    """API endpoint to get comprehensive availability data for the calendar"""
    try:
        parsed_date = dt.datetime.strptime(date, "%Y-%m-%d").date()
        availability_data = get_resource_availability_for_date(parsed_date)
        
        # Format data for frontend consumption
        formatted_data = {}
        for resource_id, avail_info in availability_data.items():
            resource = avail_info['resource']
            
            # Calculate overall availability status
            is_fully_booked = is_resource_fully_booked(resource_id, parsed_date)
            has_pass_booking = avail_info['has_day_pass_booking']
            hourly_avail = avail_info['hourly_availability']
            
            # Determine status for calendar coloring
            if is_fully_booked:
                status = 'booked'
            elif has_pass_booking or any(seats == 0 for seats in hourly_avail.values()):
                status = 'limited'
            else:
                status = 'available'
            
            formatted_data[resource_id] = {
                'name': resource.name,
                'capacity': resource.capacity,
                'status': status,
                'hourly_availability': hourly_avail,
                'has_pass_booking': has_pass_booking,
                'overflow_eligible': avail_info.get('overflow_eligible', False),
                'overflow_rate': avail_info.get('overflow_rate', None),
                'rates': {
                    'hourly': resource.hourly_rate_cents,
                    'day': resource.day_rate_cents,
                    'week': resource.week_rate_cents,
                    'month': resource.month_rate_cents
                }
            }
        
        return jsonify(formatted_data)
    
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.post("/webhook/mercury")
def mercury_webhook():
    """Handle Mercury payment webhooks"""
    try:
        event = verify_mercury_webhook(request)
        intent_id = event["data"]["id"]
        status = event["data"]["status"]
        
        if status == "paid":
            mark_payment_paid("mercury", intent_id)
        
        return "ok"
    except Exception as e:
        return str(e), 400

@app.get("/qr/<int:bid>")
def qr(bid):
    img = qrcode.make(f"{BASE_URL}/checkin/{bid}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

@app.get("/checkin/<int:bid>")
def checkin(bid):
    booking = Booking.query.get_or_404(bid)
    booking.status = "checked_in"
    db.session.commit()
    return f"Checked in booking #{booking.id} for {booking.name} – {booking.resource.name}"

# ---------------- Admin Routes ----------------
def admin_guard():
    pwd = request.args.get("key") or request.form.get("key")
    if pwd != ADMIN_PASSWORD:
        abort(401)

@app.get("/admin")
def admin_home():
    admin_guard()
    resources = Resource.query.order_by(Resource.active.desc(), Resource.name).all()
    latest_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(20).all()
    active_passes = Pass.query.filter_by(status="active").order_by(Pass.valid_to.desc()).all()
    payments = Payment.query.order_by(Payment.created_at.desc()).limit(20).all()
    
    totals = {
        "paid": sum(b.amount_cents for b in Booking.query.filter_by(status="paid")),
        "free": Booking.query.filter_by(status="free").count()
    }
    
    return render_template("admin.html", 
                         resources=resources, 
                         latest_bookings=latest_bookings,
                         active_passes=active_passes,
                         payments=payments,
                         totals=totals, 
                         key=request.args.get("key"),
                         as_money=as_money)

@app.post("/admin/resource")
def admin_resource():
    admin_guard()
    name = request.form["name"].strip()
    rate = int(float(request.form["rate"]) * 100)
    capacity = int(request.form.get("capacity", 10))
    
    resource = Resource()
    resource.name = name
    resource.hourly_rate_cents = rate
    resource.capacity = capacity
    resource.active = True
    default_hours = {
        "mon": [["09:00", "18:00"]], "tue": [["09:00", "18:00"]], 
        "wed": [["09:00", "18:00"]], "thu": [["09:00", "18:00"]], 
        "fri": [["09:00", "18:00"]], "sat": [["10:00", "14:00"]], 
        "sun": []
    }
    resource.set_opening_hours(default_hours)
    
    db.session.add(resource)
    db.session.commit()
    return redirect(url_for("admin_home", key=request.form["key"]))

@app.get("/admin/resource/<int:rid>")
def admin_resource_edit(rid):
    admin_guard()
    resource = Resource.query.get_or_404(rid)
    return render_template("admin_resource_edit.html", 
                         resource=resource, 
                         key=request.args.get("key"),
                         opening_hours=resource.get_opening_hours())

@app.post("/admin/resource/<int:rid>/update")
def admin_resource_update(rid):
    admin_guard()
    resource = Resource.query.get_or_404(rid)
    
    resource.name = request.form["name"].strip()
    resource.hourly_rate_cents = int(float(request.form["rate"]) * 100)
    resource.capacity = int(request.form["capacity"])
    resource.active = "active" in request.form
    
    # Update opening hours
    hours = {}
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    for day in days:
        open_time = request.form.get(f"{day}_open", "")
        close_time = request.form.get(f"{day}_close", "")
        if open_time and close_time:
            hours[day] = [[open_time, close_time]]
        else:
            hours[day] = []
    
    resource.set_opening_hours(hours)
    db.session.commit()
    return redirect(url_for("admin_home", key=request.form["key"]))

@app.post("/admin/resource/<int:rid>/delete")
def admin_resource_delete(rid):
    admin_guard()
    resource = Resource.query.get_or_404(rid)
    
    # Check if there are any bookings for this resource
    booking_count = Booking.query.filter_by(resource_id=rid).count()
    if booking_count > 0:
        flash(f"Cannot delete {resource.name} - it has {booking_count} bookings.", "error")
    else:
        db.session.delete(resource)
        db.session.commit()
        flash(f"Resource {resource.name} deleted successfully.", "success")
    
    return redirect(url_for("admin_home", key=request.form["key"]))

@app.post("/admin/resource-toggle")
def admin_resource_toggle():
    admin_guard()
    rid = int(request.form["rid"])
    resource = Resource.query.get_or_404(rid)
    resource.active = not resource.active
    db.session.commit()
    return redirect(url_for("admin_home", key=request.form["key"]))

@app.post("/admin/booking-cancel")
def admin_booking_cancel():
    admin_guard()
    bid = int(request.form["bid"])
    booking = Booking.query.get_or_404(bid)
    booking.status = "cancelled"
    db.session.commit()
    return redirect(url_for("admin_home", key=request.form["key"]))

@app.post("/admin/payment-mark-paid")
def admin_payment_mark_paid():
    admin_guard()
    pid = int(request.form["pid"])
    txn_id = request.form.get("txn_id", "").strip()
    
    payment = Payment.query.get_or_404(pid)
    payment.status = "paid"
    if txn_id:
        payment.intent_id = txn_id
    
    if payment.booking_id:
        booking = Booking.query.get(payment.booking_id)
        if booking:
            booking.status = "paid"
    
    if payment.pass_id:
        pass_obj = Pass.query.get(payment.pass_id)
        if pass_obj:
            pass_obj.status = "active"
    
    db.session.commit()
    return redirect(url_for("admin_home", key=request.form["key"]))

@app.post("/admin/pass-expire")
def admin_pass_expire():
    admin_guard()
    pid = int(request.form["pid"])
    pass_obj = Pass.query.get_or_404(pid)
    pass_obj.status = "expired"
    db.session.commit()
    return redirect(url_for("admin_home", key=request.form["key"]))

# Mock Mercury checkout for testing
@app.get("/mock-mercury-checkout")
def mock_mercury_checkout():
    return render_template("mock_mercury.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)