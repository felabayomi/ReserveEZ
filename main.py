import os, datetime as dt, io, json
from decimal import Decimal
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, send_file, flash
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import qrcode
import requests

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

# ---------------- Models ----------------
class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    hourly_rate_cents = db.Column(db.Integer, nullable=False)
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
    start_dt = db.Column(db.DateTime, nullable=False)
    end_dt = db.Column(db.DateTime, nullable=False)
    hours = db.Column(db.Float, nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="reserved")  # reserved, paid, cancelled, checked_in, free
    promo_applied = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    resource = db.relationship("Resource")

# ---------------- Database Setup ----------------
with app.app_context():
    db.create_all()
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
        hot_desk = Resource()
        hot_desk.name = "Hot Desk"
        hot_desk.hourly_rate_cents = 600
        hot_desk.capacity = 8
        hot_desk.opening_hours = json.dumps(default_hours)
        resources.append(hot_desk)
        
        quiet_desk = Resource()
        quiet_desk.name = "Quiet Desk"
        quiet_desk.hourly_rate_cents = 700
        quiet_desk.capacity = 6
        quiet_desk.opening_hours = json.dumps(default_hours)
        resources.append(quiet_desk)
        
        meeting_table = Resource()
        meeting_table.name = "Meeting Table (2–4)"
        meeting_table.hourly_rate_cents = 1200
        meeting_table.capacity = 4
        meeting_table.opening_hours = json.dumps(default_hours)
        resources.append(meeting_table)
        
        whole_room = Resource()
        whole_room.name = "Whole Room"
        whole_room.hourly_rate_cents = 2000
        whole_room.capacity = 1
        whole_room.opening_hours = json.dumps(default_hours)
        resources.append(whole_room)
        db.session.add_all(resources)
        db.session.commit()

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
    
    used = Booking.query.filter(
        Booking.resource_id == resource_id,
        Booking.status.in_(["reserved", "paid", "checked_in", "free"]),
        Booking.start_dt < end_dt,
        Booking.end_dt > start_dt
    ).count()
    
    return max(0, resource.capacity - used)

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

# ---------------- Routes ----------------
@app.get("/")
def index():
    resources = Resource.query.filter_by(active=True).all()
    return render_template("index.html", resources=resources)

@app.get("/book")
def book_page():
    resources = Resource.query.filter_by(active=True).all()
    date = request.args.get("date") or dt.date.today().isoformat()
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
                         allow_pos=ALLOW_POS_CHECKOUT)

@app.post("/book")
def create_booking():
    booking_type = request.form.get("booking_type", "hourly")
    email = request.form["email"].strip().lower()
    name = request.form["name"]
    
    if booking_type == "pass":
        return handle_pass_purchase(email, name)
    else:
        return handle_hourly_booking(email, name)

def handle_hourly_booking(email, name):
    """Handle hourly desk booking"""
    resource_id = int(request.form["resource_id"])
    date = request.form["date"]
    start = request.form["start_time"]
    hours = float(request.form.get("hours", "1"))
    code = request.form.get("promo", "").strip().upper()
    payment_method = request.form.get("payment_method", "online")

    res = Resource.query.get_or_404(resource_id)
    start_dt = parse_dt(date, start)
    end_dt = start_dt + dt.timedelta(hours=hours)
    amount_cents = int(res.hourly_rate_cents * hours)

    # Validate opening hours
    if not is_in_hours(res, start_dt, end_dt):
        flash("Booking time is outside opening hours.", "warning")
        return redirect(url_for("book_page"))

    # Check capacity
    if seats_left(res.id, start_dt, end_dt) <= 0:
        flash("No capacity available for this time slot.", "warning")
        return redirect(url_for("book_page"))

    # Check for overlap
    overlap = Booking.query.filter(
        Booking.resource_id == res.id,
        Booking.status.in_(["reserved", "paid", "checked_in", "free"]),
        Booking.start_dt < end_dt,
        Booking.end_dt > start_dt
    ).count()
    
    if overlap >= res.capacity:
        flash("Time slot is full. Please choose another time.", "warning")
        return redirect(url_for("book_page"))

    # Check for active pass
    user_pass = active_pass(email, start_dt)
    if user_pass:
        # Free booking with active pass
        booking = Booking()
        booking.email = email
        booking.name = name
        booking.resource_id = res.id
        booking.start_dt = start_dt
        booking.end_dt = end_dt
        booking.hours = hours
        booking.amount_cents = 0
        booking.status = "free"
        db.session.add(booking)
        db.session.commit()
        return redirect(url_for("success_free", bid=booking.id))

    # Promo: EASYWEEK = first booking free (per email)
    apply_free = (code == PROMO_CODE) and not user_has_used_promo(email, PROMO_CODE)
    
    booking = Booking()
    booking.email = email
    booking.name = name
    booking.resource_id = res.id
    booking.start_dt = start_dt
    booking.end_dt = end_dt
    booking.hours = hours
    booking.amount_cents = 0 if apply_free else amount_cents
    booking.status = "free" if apply_free else "reserved"
    booking.promo_applied = PROMO_CODE if apply_free else None
    db.session.add(booking)
    db.session.commit()

    if apply_free:
        return redirect(url_for("success_free", bid=booking.id))

    # Handle payment
    if payment_method == "pos" and ALLOW_POS_CHECKOUT:
        # Chase POS flow - create payment record
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
                f"{res.name} ({hours}h)",
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