import os, datetime as dt, io, json, uuid, hmac, hashlib
from decimal import Decimal
from flask import (Flask, render_template, request, redirect, url_for, abort,
                   jsonify, send_file, flash, session, make_response)
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import stripe
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content
from itsdangerous import URLSafeTimedSerializer
import pytz

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "restaurant-reserve-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///reservations.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
db = SQLAlchemy(app)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "noreply@tableguard.com")
SENDER_NAME = os.getenv("SENDER_NAME", "TableGuard")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@tableguard.com")

YOUR_DOMAIN = os.getenv('REPLIT_DEV_DOMAIN', os.getenv('REPLIT_DOMAINS', 'localhost:5000').split(',')[0])
BASE_URL = f"https://{YOUR_DOMAIN}"

serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

CUISINE_TYPES = [
    "American", "Italian", "Mexican", "Chinese", "Japanese", "Indian",
    "Thai", "French", "Mediterranean", "Korean", "Vietnamese", "Greek",
    "Spanish", "Middle Eastern", "Caribbean", "Brazilian", "Ethiopian",
    "Seafood", "Steakhouse", "Vegetarian/Vegan", "Fusion", "Other"
]

TIME_SLOTS_INTERVAL = 30


# ──────────────── Models ────────────────

class Restaurant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200), unique=True, nullable=False)
    description = db.Column(db.Text)
    cuisine_type = db.Column(db.String(100))
    address = db.Column(db.String(500))
    phone = db.Column(db.String(50))
    email = db.Column(db.String(200))
    image_url = db.Column(db.String(500))
    opening_hours = db.Column(db.Text)
    slot_duration_minutes = db.Column(db.Integer, default=90)
    max_party_size = db.Column(db.Integer, default=12)
    deposit_type = db.Column(db.String(20), default="per_person")
    deposit_amount_cents = db.Column(db.Integer, default=1000)
    require_deposit = db.Column(db.Boolean, default=True)
    require_card_hold = db.Column(db.Boolean, default=True)
    cancellation_cutoff_hours = db.Column(db.Integer, default=24)
    no_show_fee_cents = db.Column(db.Integer, default=2500)
    late_cancel_fee_cents = db.Column(db.Integer, default=1500)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    tables = db.relationship("Table", backref="restaurant", lazy=True)
    reservations = db.relationship("Reservation", backref="restaurant", lazy=True)

    def get_opening_hours(self):
        if self.opening_hours:
            try:
                return json.loads(self.opening_hours)
            except:
                pass
        return {
            "mon": [["11:00", "14:00"], ["17:00", "22:00"]],
            "tue": [["11:00", "14:00"], ["17:00", "22:00"]],
            "wed": [["11:00", "14:00"], ["17:00", "22:00"]],
            "thu": [["11:00", "14:00"], ["17:00", "22:00"]],
            "fri": [["11:00", "14:00"], ["17:00", "23:00"]],
            "sat": [["10:00", "15:00"], ["17:00", "23:00"]],
            "sun": [["10:00", "15:00"], ["17:00", "21:00"]],
        }

    def get_deposit_for_party(self, party_size):
        if not self.require_deposit:
            return 0
        if self.deposit_type == "per_person":
            return self.deposit_amount_cents * party_size
        return self.deposit_amount_cents


class Table(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurant.id"), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    capacity = db.Column(db.Integer, nullable=False, default=4)
    table_type = db.Column(db.String(50), default="standard")
    active = db.Column(db.Boolean, default=True)


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, default=lambda: str(uuid.uuid4()))
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurant.id"), nullable=False)
    table_id = db.Column(db.Integer, db.ForeignKey("table.id"), nullable=True)
    guest_name = db.Column(db.String(200), nullable=False)
    guest_email = db.Column(db.String(200), nullable=False, index=True)
    guest_phone = db.Column(db.String(50), nullable=False)
    party_size = db.Column(db.Integer, nullable=False)
    reservation_date = db.Column(db.Date, nullable=False)
    reservation_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    special_requests = db.Column(db.Text)
    status = db.Column(db.String(20), default="confirmed")
    deposit_amount_cents = db.Column(db.Integer, default=0)
    deposit_paid = db.Column(db.Boolean, default=False)
    stripe_payment_intent_id = db.Column(db.String(200))
    stripe_setup_intent_id = db.Column(db.String(200))
    stripe_payment_method_id = db.Column(db.String(200))
    promo_code_id = db.Column(db.Integer, db.ForeignKey("promo_code.id"), nullable=True)
    discount_amount_cents = db.Column(db.Integer, default=0)
    no_show_fee_charged = db.Column(db.Boolean, default=False)
    no_show_fee_amount_cents = db.Column(db.Integer, default=0)
    reminder_24h_sent = db.Column(db.Boolean, default=False)
    reminder_2h_sent = db.Column(db.Boolean, default=False)
    guest_confirmed = db.Column(db.Boolean, default=False)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    table = db.relationship("Table", backref="reservations")
    promo_code = db.relationship("PromoCode", backref="reservations")

    def get_manage_token(self):
        return serializer.dumps(self.uuid, salt="manage-reservation")

    @staticmethod
    def verify_manage_token(token, max_age=604800):
        try:
            return serializer.loads(token, salt="manage-reservation", max_age=max_age)
        except:
            return None

    @property
    def reservation_datetime(self):
        return dt.datetime.combine(self.reservation_date, self.reservation_time)

    @property
    def can_cancel_free(self):
        cutoff = self.reservation_datetime - dt.timedelta(hours=self.restaurant.cancellation_cutoff_hours)
        return dt.datetime.utcnow() < cutoff

    @property
    def is_upcoming(self):
        return self.reservation_datetime > dt.datetime.utcnow()


class WaitlistEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurant.id"), nullable=False)
    guest_name = db.Column(db.String(200), nullable=False)
    guest_email = db.Column(db.String(200), nullable=False)
    guest_phone = db.Column(db.String(50), nullable=False)
    party_size = db.Column(db.Integer, nullable=False)
    desired_date = db.Column(db.Date, nullable=False)
    desired_time = db.Column(db.Time, nullable=True)
    status = db.Column(db.String(20), default="waiting")
    notified_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    restaurant = db.relationship("Restaurant", backref="waitlist_entries")


class NoShowRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    guest_email = db.Column(db.String(200), nullable=False, index=True)
    guest_phone = db.Column(db.String(50))
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurant.id"), nullable=False)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"), nullable=False)
    fee_charged_cents = db.Column(db.Integer, default=0)
    occurred_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    restaurant = db.relationship("Restaurant", backref="no_show_records")
    reservation = db.relationship("Reservation", backref="no_show_records")


class PromoCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    discount_type = db.Column(db.String(20), nullable=False, default="percent")
    discount_value = db.Column(db.Integer, nullable=False, default=10)
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurant.id"), nullable=True)
    waive_deposit = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    max_uses = db.Column(db.Integer, nullable=True)
    current_uses = db.Column(db.Integer, default=0)
    valid_from = db.Column(db.Date, nullable=True)
    valid_to = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    linked_restaurant = db.relationship("Restaurant", backref="promo_codes")


class NotificationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"), nullable=True)
    waitlist_entry_id = db.Column(db.Integer, db.ForeignKey("waitlist_entry.id"), nullable=True)
    notification_type = db.Column(db.String(50), nullable=False)
    recipient_email = db.Column(db.String(200))
    status = db.Column(db.String(20), default="sent")
    sent_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    reservation = db.relationship("Reservation", backref="notifications")


# ──────────────── Helpers ────────────────

def as_money(cents):
    if cents is None:
        return "$0.00"
    return f"${cents / 100:.2f}"

def make_slug(name):
    slug = name.lower().strip()
    slug = "".join(c if c.isalnum() or c == " " else "" for c in slug)
    slug = "-".join(slug.split())
    return slug

def generate_time_slots(opening_hours_for_day, interval=TIME_SLOTS_INTERVAL):
    slots = []
    for period in opening_hours_for_day:
        start = dt.datetime.strptime(period[0], "%H:%M")
        end = dt.datetime.strptime(period[1], "%H:%M")
        current = start
        while current < end:
            slots.append(current.strftime("%H:%M"))
            current += dt.timedelta(minutes=interval)
    return slots

def get_day_key(date_obj):
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return days[date_obj.weekday()]

def find_available_tables(restaurant_id, date, start_time, end_time, party_size, exclude_reservation_id=None):
    tables = Table.query.filter_by(
        restaurant_id=restaurant_id, active=True
    ).filter(Table.capacity >= party_size).order_by(Table.capacity.asc()).all()

    available = []
    for table in tables:
        conflicts = Reservation.query.filter(
            Reservation.table_id == table.id,
            Reservation.reservation_date == date,
            Reservation.status.in_(["confirmed", "seated"]),
            Reservation.reservation_time < end_time,
            Reservation.end_time > start_time,
        )
        if exclude_reservation_id:
            conflicts = conflicts.filter(Reservation.id != exclude_reservation_id)
        if conflicts.count() == 0:
            available.append(table)
    return available

def get_no_show_count(email):
    return NoShowRecord.query.filter_by(guest_email=email.lower().strip()).count()

def is_repeat_no_show(email, threshold=2):
    return get_no_show_count(email) >= threshold

def calculate_end_time(start_time, duration_minutes):
    start_dt = dt.datetime.combine(dt.date.today(), start_time)
    end_dt = start_dt + dt.timedelta(minutes=duration_minutes)
    return end_dt.time()


# ──────────────── Email Helpers ────────────────

def send_email(to_email, subject, html_content):
    if not SENDGRID_API_KEY:
        print(f"[EMAIL SKIP] No SendGrid key. Would send to {to_email}: {subject}")
        return False
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        message = Mail(
            from_email=Email(SENDER_EMAIL, SENDER_NAME),
            to_emails=To(to_email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        sg.send(message)
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False

def send_confirmation_email(reservation):
    manage_url = f"{BASE_URL}/manage/{reservation.uuid}/{reservation.get_manage_token()}"
    calendar_url = f"{BASE_URL}/calendar/{reservation.uuid}.ics"
    html = render_template("emails/confirmation.html",
                           r=reservation, manage_url=manage_url,
                           calendar_url=calendar_url, as_money=as_money)
    success = send_email(reservation.guest_email,
                         f"Reservation Confirmed - {reservation.restaurant.name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="confirmation",
                              recipient_email=reservation.guest_email)
        db.session.add(log)
        db.session.commit()

def send_reminder_email(reservation, hours_before):
    manage_url = f"{BASE_URL}/manage/{reservation.uuid}/{reservation.get_manage_token()}"
    confirm_url = f"{BASE_URL}/confirm-attendance/{reservation.uuid}/{reservation.get_manage_token()}"
    html = render_template("emails/reminder.html",
                           r=reservation, hours_before=hours_before,
                           manage_url=manage_url, confirm_url=confirm_url,
                           as_money=as_money)
    success = send_email(reservation.guest_email,
                         f"Reminder: Reservation at {reservation.restaurant.name} in {hours_before} hours",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type=f"reminder_{hours_before}h",
                              recipient_email=reservation.guest_email)
        db.session.add(log)
        db.session.commit()

def send_cancellation_email(reservation, fee_charged=False, fee_amount=0):
    html = render_template("emails/cancellation.html",
                           r=reservation, fee_charged=fee_charged,
                           fee_amount=fee_amount, as_money=as_money)
    success = send_email(reservation.guest_email,
                         f"Reservation Cancelled - {reservation.restaurant.name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="cancellation",
                              recipient_email=reservation.guest_email)
        db.session.add(log)
        db.session.commit()

def send_no_show_email(reservation, fee_amount):
    html = render_template("emails/no_show.html",
                           r=reservation, fee_amount=fee_amount,
                           as_money=as_money)
    success = send_email(reservation.guest_email,
                         f"No-Show Notice - {reservation.restaurant.name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="no_show",
                              recipient_email=reservation.guest_email)
        db.session.add(log)
        db.session.commit()

def send_waitlist_notification(entry, reservation_slots):
    reserve_url = f"{BASE_URL}/reserve/{entry.restaurant.slug}?date={entry.desired_date}&party_size={entry.party_size}"
    html = render_template("emails/waitlist_available.html",
                           entry=entry, reserve_url=reserve_url,
                           slots=reservation_slots, as_money=as_money)
    success = send_email(entry.guest_email,
                         f"Table Available at {entry.restaurant.name}!",
                         html)
    if success:
        log = NotificationLog(waitlist_entry_id=entry.id,
                              notification_type="waitlist_available",
                              recipient_email=entry.guest_email)
        db.session.add(log)
        entry.status = "notified"
        entry.notified_at = dt.datetime.utcnow()
        entry.expires_at = dt.datetime.utcnow() + dt.timedelta(hours=2)
        db.session.commit()


# ──────────────── Database Init ────────────────

def initialize_database():
    with app.app_context():
        db.create_all()

        if Restaurant.query.count() == 0:
            r1 = Restaurant(
                name="The Golden Fork",
                slug="the-golden-fork",
                description="A fine dining experience with a modern twist on classic American cuisine. Our chef-driven menu features locally sourced ingredients and seasonal specialties.",
                cuisine_type="American",
                address="123 Main Street, Downtown",
                phone="(555) 123-4567",
                email="info@goldenfork.com",
                opening_hours=json.dumps({
                    "mon": [["11:00", "14:00"], ["17:00", "22:00"]],
                    "tue": [["11:00", "14:00"], ["17:00", "22:00"]],
                    "wed": [["11:00", "14:00"], ["17:00", "22:00"]],
                    "thu": [["11:00", "14:00"], ["17:00", "22:00"]],
                    "fri": [["11:00", "14:00"], ["17:00", "23:00"]],
                    "sat": [["10:00", "15:00"], ["17:00", "23:00"]],
                    "sun": [["10:00", "15:00"], ["17:00", "21:00"]],
                }),
                slot_duration_minutes=90,
                max_party_size=10,
                deposit_type="per_person",
                deposit_amount_cents=1000,
                require_deposit=True,
                require_card_hold=True,
                cancellation_cutoff_hours=24,
                no_show_fee_cents=2500,
                late_cancel_fee_cents=1500,
            )
            db.session.add(r1)
            db.session.flush()

            tables_r1 = [
                Table(restaurant_id=r1.id, name="Table 1", capacity=2, table_type="window"),
                Table(restaurant_id=r1.id, name="Table 2", capacity=2, table_type="window"),
                Table(restaurant_id=r1.id, name="Table 3", capacity=4, table_type="standard"),
                Table(restaurant_id=r1.id, name="Table 4", capacity=4, table_type="standard"),
                Table(restaurant_id=r1.id, name="Table 5", capacity=6, table_type="booth"),
                Table(restaurant_id=r1.id, name="Table 6", capacity=8, table_type="private"),
            ]
            db.session.add_all(tables_r1)

            r2 = Restaurant(
                name="Sakura Garden",
                slug="sakura-garden",
                description="Authentic Japanese cuisine featuring fresh sushi, ramen, and traditional dishes prepared by our master chef with over 20 years of experience.",
                cuisine_type="Japanese",
                address="456 Oak Avenue, Midtown",
                phone="(555) 234-5678",
                email="info@sakuragarden.com",
                opening_hours=json.dumps({
                    "mon": [],
                    "tue": [["11:30", "14:00"], ["17:00", "22:00"]],
                    "wed": [["11:30", "14:00"], ["17:00", "22:00"]],
                    "thu": [["11:30", "14:00"], ["17:00", "22:00"]],
                    "fri": [["11:30", "14:00"], ["17:00", "23:00"]],
                    "sat": [["11:00", "15:00"], ["17:00", "23:00"]],
                    "sun": [["11:00", "15:00"], ["17:00", "21:00"]],
                }),
                slot_duration_minutes=120,
                max_party_size=8,
                deposit_type="flat",
                deposit_amount_cents=2000,
                require_deposit=True,
                require_card_hold=True,
                cancellation_cutoff_hours=12,
                no_show_fee_cents=3000,
                late_cancel_fee_cents=2000,
            )
            db.session.add(r2)
            db.session.flush()

            tables_r2 = [
                Table(restaurant_id=r2.id, name="Counter 1", capacity=2, table_type="counter"),
                Table(restaurant_id=r2.id, name="Counter 2", capacity=2, table_type="counter"),
                Table(restaurant_id=r2.id, name="Table A", capacity=4, table_type="standard"),
                Table(restaurant_id=r2.id, name="Table B", capacity=4, table_type="standard"),
                Table(restaurant_id=r2.id, name="Tatami Room", capacity=6, table_type="private"),
                Table(restaurant_id=r2.id, name="Large Tatami", capacity=8, table_type="private"),
            ]
            db.session.add_all(tables_r2)

            r3 = Restaurant(
                name="Casa Bella",
                slug="casa-bella",
                description="Warm and inviting Italian restaurant serving handmade pasta, wood-fired pizza, and a curated selection of Italian wines in a rustic Tuscan setting.",
                cuisine_type="Italian",
                address="789 Elm Street, Uptown",
                phone="(555) 345-6789",
                email="info@casabella.com",
                opening_hours=json.dumps({
                    "mon": [["17:00", "22:00"]],
                    "tue": [["17:00", "22:00"]],
                    "wed": [["11:00", "14:00"], ["17:00", "22:00"]],
                    "thu": [["11:00", "14:00"], ["17:00", "22:00"]],
                    "fri": [["11:00", "14:00"], ["17:00", "23:00"]],
                    "sat": [["11:00", "23:00"]],
                    "sun": [["11:00", "21:00"]],
                }),
                slot_duration_minutes=105,
                max_party_size=12,
                deposit_type="per_person",
                deposit_amount_cents=1500,
                require_deposit=True,
                require_card_hold=True,
                cancellation_cutoff_hours=24,
                no_show_fee_cents=2500,
                late_cancel_fee_cents=1500,
            )
            db.session.add(r3)
            db.session.flush()

            tables_r3 = [
                Table(restaurant_id=r3.id, name="Patio 1", capacity=2, table_type="patio"),
                Table(restaurant_id=r3.id, name="Patio 2", capacity=2, table_type="patio"),
                Table(restaurant_id=r3.id, name="Indoor 1", capacity=4, table_type="standard"),
                Table(restaurant_id=r3.id, name="Indoor 2", capacity=4, table_type="standard"),
                Table(restaurant_id=r3.id, name="Indoor 3", capacity=6, table_type="booth"),
                Table(restaurant_id=r3.id, name="Private Dining", capacity=12, table_type="private"),
            ]
            db.session.add_all(tables_r3)

            promo1 = PromoCode(code="WELCOME10", discount_type="percent", discount_value=10,
                               active=True, waive_deposit=False)
            promo2 = PromoCode(code="FIRSTVISIT", discount_type="percent", discount_value=100,
                               active=True, waive_deposit=True, max_uses=100)
            db.session.add_all([promo1, promo2])

            db.session.commit()
            print("Database initialized with sample restaurants and tables.")

try:
    initialize_database()
except Exception as e:
    print(f"Warning: Database initialization: {e}")
    try:
        with app.app_context():
            db.create_all()
    except Exception as e2:
        print(f"Error creating tables: {e2}")


# ──────────────── Template Filters ────────────────

@app.template_filter('money')
def money_filter(cents):
    return as_money(cents)

@app.template_filter('time_fmt')
def time_fmt_filter(t):
    if isinstance(t, dt.time):
        return t.strftime("%-I:%M %p")
    return str(t)

@app.template_filter('date_fmt')
def date_fmt_filter(d):
    if isinstance(d, (dt.date, dt.datetime)):
        return d.strftime("%B %d, %Y")
    return str(d)

@app.after_request
def add_cache_headers(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ──────────────── Public Routes ────────────────

@app.route("/")
def index():
    restaurants = Restaurant.query.filter_by(active=True).order_by(Restaurant.name).all()
    cuisine_filter = request.args.get("cuisine", "")
    if cuisine_filter:
        restaurants = [r for r in restaurants if r.cuisine_type == cuisine_filter]
    return render_template("index.html", restaurants=restaurants,
                           cuisine_types=CUISINE_TYPES, cuisine_filter=cuisine_filter,
                           as_money=as_money)

@app.route("/restaurant/<slug>")
def restaurant_detail(slug):
    restaurant = Restaurant.query.filter_by(slug=slug, active=True).first_or_404()
    today = dt.date.today()
    selected_date = request.args.get("date", today.isoformat())
    try:
        selected_date = dt.date.fromisoformat(selected_date)
    except:
        selected_date = today
    if selected_date < today:
        selected_date = today

    party_size = request.args.get("party_size", 2, type=int)
    party_size = max(1, min(party_size, restaurant.max_party_size))

    day_key = get_day_key(selected_date)
    hours = restaurant.get_opening_hours()
    day_hours = hours.get(day_key, [])

    available_slots = []
    if day_hours:
        all_slots = generate_time_slots(day_hours)
        for slot_str in all_slots:
            slot_time = dt.datetime.strptime(slot_str, "%H:%M").time()
            if selected_date == today and slot_time <= dt.datetime.now().time():
                continue
            end_time = calculate_end_time(slot_time, restaurant.slot_duration_minutes)
            tables = find_available_tables(restaurant.id, selected_date, slot_time, end_time, party_size)
            available_slots.append({
                "time": slot_str,
                "time_display": dt.datetime.strptime(slot_str, "%H:%M").strftime("%-I:%M %p"),
                "available": len(tables) > 0,
                "tables_left": len(tables)
            })

    dates = []
    for i in range(14):
        d = today + dt.timedelta(days=i)
        dk = get_day_key(d)
        dh = hours.get(dk, [])
        dates.append({"date": d, "open": len(dh) > 0, "day_name": d.strftime("%a"),
                       "day_num": d.strftime("%d"), "month": d.strftime("%b")})

    return render_template("restaurant.html", restaurant=restaurant,
                           selected_date=selected_date, party_size=party_size,
                           available_slots=available_slots, dates=dates,
                           as_money=as_money, today=today)


@app.route("/reserve/<slug>", methods=["GET", "POST"])
def reserve(slug):
    restaurant = Restaurant.query.filter_by(slug=slug, active=True).first_or_404()

    if request.method == "GET":
        date_str = request.args.get("date")
        time_str = request.args.get("time")
        party_size = request.args.get("party_size", 2, type=int)

        if not date_str or not time_str:
            return redirect(url_for("restaurant_detail", slug=slug))

        try:
            res_date = dt.date.fromisoformat(date_str)
            res_time = dt.datetime.strptime(time_str, "%H:%M").time()
        except:
            return redirect(url_for("restaurant_detail", slug=slug))

        end_time = calculate_end_time(res_time, restaurant.slot_duration_minutes)
        tables = find_available_tables(restaurant.id, res_date, res_time, end_time, party_size)

        if not tables:
            flash("Sorry, that time slot is no longer available.", "error")
            return redirect(url_for("restaurant_detail", slug=slug,
                                    date=date_str, party_size=party_size))

        deposit_amount = restaurant.get_deposit_for_party(party_size)
        no_show_count = 0
        force_deposit = False

        return render_template("reserve.html", restaurant=restaurant,
                               res_date=res_date, res_time=res_time,
                               end_time=end_time, party_size=party_size,
                               deposit_amount=deposit_amount,
                               stripe_public_key=STRIPE_PUBLIC_KEY,
                               no_show_count=no_show_count,
                               force_deposit=force_deposit,
                               as_money=as_money)

    # POST - Create reservation
    try:
        guest_name = request.form.get("guest_name", "").strip()
        guest_email = request.form.get("guest_email", "").strip().lower()
        guest_phone = request.form.get("guest_phone", "").strip()
        party_size = int(request.form.get("party_size", 2))
        date_str = request.form.get("date")
        time_str = request.form.get("time")
        special_requests = request.form.get("special_requests", "").strip()
        promo_code_str = request.form.get("promo_code", "").strip().upper()
        payment_method_id = request.form.get("payment_method_id", "")
        setup_intent_id = request.form.get("setup_intent_id", "")

        if not all([guest_name, guest_email, guest_phone, date_str, time_str]):
            flash("Please fill in all required fields.", "error")
            return redirect(url_for("reserve", slug=slug, date=date_str,
                                    time=time_str, party_size=party_size))

        res_date = dt.date.fromisoformat(date_str)
        res_time = dt.datetime.strptime(time_str, "%H:%M").time()
        end_time = calculate_end_time(res_time, restaurant.slot_duration_minutes)

        tables = find_available_tables(restaurant.id, res_date, res_time, end_time, party_size)
        if not tables:
            flash("Sorry, that time slot is no longer available.", "error")
            return redirect(url_for("restaurant_detail", slug=slug))

        assigned_table = tables[0]
        deposit_amount = restaurant.get_deposit_for_party(party_size)
        discount_cents = 0
        promo_id = None
        waive_deposit = False

        if promo_code_str:
            promo = PromoCode.query.filter_by(code=promo_code_str, active=True).first()
            if promo:
                if promo.restaurant_id and promo.restaurant_id != restaurant.id:
                    promo = None
                elif promo.max_uses and promo.current_uses >= promo.max_uses:
                    promo = None
                elif promo.valid_to and dt.date.today() > promo.valid_to:
                    promo = None
                elif promo.valid_from and dt.date.today() < promo.valid_from:
                    promo = None
            if promo:
                promo_id = promo.id
                waive_deposit = promo.waive_deposit
                if promo.discount_type == "percent":
                    discount_cents = int(deposit_amount * promo.discount_value / 100)
                elif promo.discount_type == "flat":
                    discount_cents = min(promo.discount_value, deposit_amount)
                elif promo.discount_type == "free":
                    discount_cents = deposit_amount
                    waive_deposit = True
                promo.current_uses += 1

        actual_deposit = max(0, deposit_amount - discount_cents)
        no_show_count = get_no_show_count(guest_email)
        if no_show_count >= 2:
            waive_deposit = False
            actual_deposit = max(actual_deposit, restaurant.no_show_fee_cents)

        reservation = Reservation(
            restaurant_id=restaurant.id,
            table_id=assigned_table.id,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_phone=guest_phone,
            party_size=party_size,
            reservation_date=res_date,
            reservation_time=res_time,
            end_time=end_time,
            special_requests=special_requests,
            status="confirmed",
            deposit_amount_cents=actual_deposit if not waive_deposit else 0,
            promo_code_id=promo_id,
            discount_amount_cents=discount_cents,
            stripe_setup_intent_id=setup_intent_id if setup_intent_id else None,
            stripe_payment_method_id=payment_method_id if payment_method_id else None,
        )

        if actual_deposit > 0 and not waive_deposit and payment_method_id and stripe.api_key:
            try:
                intent = stripe.PaymentIntent.create(
                    amount=actual_deposit,
                    currency="usd",
                    payment_method=payment_method_id,
                    confirm=True,
                    automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                    metadata={"reservation_uuid": reservation.uuid,
                              "restaurant": restaurant.name}
                )
                reservation.stripe_payment_intent_id = intent.id
                reservation.deposit_paid = True
            except stripe.StripeError as e:
                flash(f"Payment failed: {str(e)}", "error")
                return redirect(url_for("reserve", slug=slug, date=date_str,
                                        time=time_str, party_size=party_size))
        elif actual_deposit == 0 or waive_deposit:
            reservation.deposit_paid = True

        db.session.add(reservation)
        db.session.commit()

        try:
            send_confirmation_email(reservation)
        except Exception as e:
            print(f"Email error: {e}")

        return redirect(url_for("confirmation", res_uuid=reservation.uuid))

    except Exception as e:
        db.session.rollback()
        print(f"Reservation error: {e}")
        flash("An error occurred creating your reservation. Please try again.", "error")
        return redirect(url_for("restaurant_detail", slug=slug))


@app.route("/confirmation/<res_uuid>")
def confirmation(res_uuid):
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()
    manage_url = f"{BASE_URL}/manage/{reservation.uuid}/{reservation.get_manage_token()}"
    calendar_url = f"{BASE_URL}/calendar/{reservation.uuid}.ics"
    return render_template("confirmation.html", r=reservation,
                           manage_url=manage_url, calendar_url=calendar_url,
                           as_money=as_money)


@app.route("/manage/<res_uuid>/<token>")
def manage_reservation(res_uuid, token):
    verified_uuid = Reservation.verify_manage_token(token)
    if not verified_uuid or verified_uuid != res_uuid:
        abort(403)
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()
    return render_template("manage.html", r=reservation, token=token, as_money=as_money)


@app.route("/cancel/<res_uuid>/<token>", methods=["POST"])
def cancel_reservation(res_uuid, token):
    verified_uuid = Reservation.verify_manage_token(token)
    if not verified_uuid or verified_uuid != res_uuid:
        abort(403)
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()

    if reservation.status in ["cancelled", "no_show", "completed"]:
        flash("This reservation cannot be cancelled.", "error")
        return redirect(url_for("manage_reservation", res_uuid=res_uuid, token=token))

    fee_charged = False
    fee_amount = 0

    if reservation.can_cancel_free:
        if reservation.deposit_paid and reservation.stripe_payment_intent_id and stripe.api_key:
            try:
                stripe.Refund.create(payment_intent=reservation.stripe_payment_intent_id)
            except Exception as e:
                print(f"Refund error: {e}")
    else:
        fee_amount = reservation.restaurant.late_cancel_fee_cents
        fee_charged = True
        if reservation.deposit_paid and reservation.deposit_amount_cents > 0:
            pass

    reservation.status = "cancelled"
    reservation.cancelled_at = dt.datetime.utcnow()
    db.session.commit()

    notify_waitlist_for_slot(reservation.restaurant_id, reservation.reservation_date,
                             reservation.reservation_time, reservation.party_size)

    try:
        send_cancellation_email(reservation, fee_charged, fee_amount)
    except Exception as e:
        print(f"Email error: {e}")

    flash("Your reservation has been cancelled.", "success")
    return redirect(url_for("manage_reservation", res_uuid=res_uuid, token=token))


@app.route("/confirm-attendance/<res_uuid>/<token>")
def confirm_attendance(res_uuid, token):
    verified_uuid = Reservation.verify_manage_token(token)
    if not verified_uuid or verified_uuid != res_uuid:
        abort(403)
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()
    reservation.guest_confirmed = True
    db.session.commit()
    flash("Thank you for confirming! We look forward to seeing you.", "success")
    return redirect(url_for("manage_reservation", res_uuid=res_uuid, token=token))


@app.route("/calendar/<res_uuid>.ics")
def calendar_export(res_uuid):
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()
    r = reservation.restaurant

    start_dt = dt.datetime.combine(reservation.reservation_date, reservation.reservation_time)
    end_dt = dt.datetime.combine(reservation.reservation_date, reservation.end_time)

    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//TableGuard//Reservation//EN
BEGIN:VEVENT
DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}
DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}
SUMMARY:Dinner at {r.name}
DESCRIPTION:Reservation for {reservation.party_size} at {r.name}\\nConfirmation: {reservation.uuid[:8].upper()}
LOCATION:{r.address}
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR"""

    response = make_response(ics)
    response.headers["Content-Type"] = "text/calendar"
    response.headers["Content-Disposition"] = f"attachment; filename=reservation-{reservation.uuid[:8]}.ics"
    return response


@app.route("/waitlist/<slug>", methods=["POST"])
def join_waitlist(slug):
    restaurant = Restaurant.query.filter_by(slug=slug, active=True).first_or_404()
    entry = WaitlistEntry(
        restaurant_id=restaurant.id,
        guest_name=request.form.get("guest_name", "").strip(),
        guest_email=request.form.get("guest_email", "").strip().lower(),
        guest_phone=request.form.get("guest_phone", "").strip(),
        party_size=int(request.form.get("party_size", 2)),
        desired_date=dt.date.fromisoformat(request.form.get("desired_date")),
        desired_time=dt.datetime.strptime(request.form.get("desired_time", "19:00"), "%H:%M").time() if request.form.get("desired_time") else None,
    )
    db.session.add(entry)
    db.session.commit()
    flash("You've been added to the waitlist! We'll notify you if a spot opens up.", "success")
    return redirect(url_for("restaurant_detail", slug=slug,
                            date=entry.desired_date.isoformat(),
                            party_size=entry.party_size))


# ──────────────── Payment Routes ────────────────

@app.route("/create-setup-intent", methods=["POST"])
def create_setup_intent():
    if not stripe.api_key:
        return jsonify({"error": "Payments not configured"}), 400
    try:
        intent = stripe.SetupIntent.create(
            payment_method_types=["card"],
        )
        return jsonify({"clientSecret": intent.client_secret})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/validate-promo", methods=["POST"])
def validate_promo():
    data = request.get_json()
    code = data.get("code", "").strip().upper()
    restaurant_id = data.get("restaurant_id")
    party_size = data.get("party_size", 2)

    promo = PromoCode.query.filter_by(code=code, active=True).first()
    if not promo:
        return jsonify({"valid": False, "message": "Invalid promo code"})

    if promo.restaurant_id and str(promo.restaurant_id) != str(restaurant_id):
        return jsonify({"valid": False, "message": "This code is not valid for this restaurant"})
    if promo.max_uses and promo.current_uses >= promo.max_uses:
        return jsonify({"valid": False, "message": "This code has expired"})
    if promo.valid_to and dt.date.today() > promo.valid_to:
        return jsonify({"valid": False, "message": "This code has expired"})

    restaurant = Restaurant.query.get(restaurant_id)
    deposit = restaurant.get_deposit_for_party(party_size) if restaurant else 0

    if promo.discount_type == "percent":
        discount = int(deposit * promo.discount_value / 100)
    elif promo.discount_type == "flat":
        discount = min(promo.discount_value, deposit)
    else:
        discount = deposit

    return jsonify({
        "valid": True,
        "discount_type": promo.discount_type,
        "discount_value": promo.discount_value,
        "discount_amount": discount,
        "waive_deposit": promo.waive_deposit,
        "message": f"{promo.discount_value}% off" if promo.discount_type == "percent" else "Deposit waived" if promo.waive_deposit else f"${discount/100:.2f} off"
    })


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    if STRIPE_WEBHOOK_SECRET and sig_header:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception:
            return "", 400
    else:
        event = json.loads(payload)

    if event.get("type") == "payment_intent.succeeded":
        pi = event["data"]["object"]
        res_uuid = pi.get("metadata", {}).get("reservation_uuid")
        if res_uuid:
            reservation = Reservation.query.filter_by(uuid=res_uuid).first()
            if reservation:
                reservation.deposit_paid = True
                db.session.commit()

    return "", 200


# ──────────────── Waitlist Helper ────────────────

def notify_waitlist_for_slot(restaurant_id, date, time, party_size):
    entries = WaitlistEntry.query.filter(
        WaitlistEntry.restaurant_id == restaurant_id,
        WaitlistEntry.desired_date == date,
        WaitlistEntry.status == "waiting",
        WaitlistEntry.party_size <= party_size + 2,
    ).order_by(WaitlistEntry.created_at.asc()).all()

    for entry in entries:
        try:
            send_waitlist_notification(entry, [time.strftime("%-I:%M %p")])
        except Exception as e:
            print(f"Waitlist notification error: {e}")


# ──────────────── Cron Routes ────────────────

@app.route("/cron/send-reminders")
def cron_send_reminders():
    now = dt.datetime.utcnow()
    sent_count = 0

    upcoming_24h = Reservation.query.filter(
        Reservation.status == "confirmed",
        Reservation.reminder_24h_sent == False,
        Reservation.reservation_date <= (now + dt.timedelta(hours=25)).date(),
        Reservation.reservation_date >= now.date(),
    ).all()

    for r in upcoming_24h:
        res_dt = dt.datetime.combine(r.reservation_date, r.reservation_time)
        hours_until = (res_dt - now).total_seconds() / 3600
        if 23 <= hours_until <= 25:
            try:
                send_reminder_email(r, 24)
                r.reminder_24h_sent = True
                sent_count += 1
            except Exception as e:
                print(f"Reminder error: {e}")

    upcoming_2h = Reservation.query.filter(
        Reservation.status == "confirmed",
        Reservation.reminder_2h_sent == False,
        Reservation.reservation_date == now.date(),
    ).all()

    for r in upcoming_2h:
        res_dt = dt.datetime.combine(r.reservation_date, r.reservation_time)
        hours_until = (res_dt - now).total_seconds() / 3600
        if 1.5 <= hours_until <= 2.5:
            try:
                send_reminder_email(r, 2)
                r.reminder_2h_sent = True
                sent_count += 1
            except Exception as e:
                print(f"Reminder error: {e}")

    db.session.commit()
    return jsonify({"reminders_sent": sent_count})


@app.route("/cron/process-no-shows")
def cron_process_no_shows():
    now = dt.datetime.utcnow()
    grace_period = dt.timedelta(minutes=30)
    processed = 0

    past_reservations = Reservation.query.filter(
        Reservation.status == "confirmed",
        Reservation.reservation_date <= now.date(),
    ).all()

    for r in past_reservations:
        res_dt = dt.datetime.combine(r.reservation_date, r.reservation_time)
        if now > res_dt + grace_period:
            r.status = "no_show"

            record = NoShowRecord(
                guest_email=r.guest_email,
                guest_phone=r.guest_phone,
                restaurant_id=r.restaurant_id,
                reservation_id=r.id,
            )

            fee_amount = r.restaurant.no_show_fee_cents
            if r.stripe_payment_method_id and stripe.api_key and fee_amount > 0:
                try:
                    charge = stripe.PaymentIntent.create(
                        amount=fee_amount,
                        currency="usd",
                        payment_method=r.stripe_payment_method_id,
                        confirm=True,
                        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                        metadata={"type": "no_show_fee",
                                  "reservation_uuid": r.uuid,
                                  "restaurant": r.restaurant.name}
                    )
                    r.no_show_fee_charged = True
                    r.no_show_fee_amount_cents = fee_amount
                    record.fee_charged_cents = fee_amount
                except Exception as e:
                    print(f"No-show charge error: {e}")

            db.session.add(record)
            processed += 1

            try:
                send_no_show_email(r, fee_amount)
            except:
                pass

    db.session.commit()
    return jsonify({"no_shows_processed": processed})


@app.route("/cron/expire-waitlist")
def cron_expire_waitlist():
    now = dt.datetime.utcnow()
    expired = WaitlistEntry.query.filter(
        WaitlistEntry.status == "notified",
        WaitlistEntry.expires_at < now,
    ).all()
    for entry in expired:
        entry.status = "expired"
    past = WaitlistEntry.query.filter(
        WaitlistEntry.status == "waiting",
        WaitlistEntry.desired_date < now.date(),
    ).all()
    for entry in past:
        entry.status = "expired"
    db.session.commit()
    return jsonify({"expired": len(expired) + len(past)})


# ──────────────── Admin Routes ────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid password.", "error")
    return render_template("admin/login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))

def require_admin():
    if not session.get("admin"):
        abort(redirect(url_for("admin_login")))

@app.route("/admin")
def admin_dashboard():
    require_admin()
    today = dt.date.today()
    restaurants = Restaurant.query.filter_by(active=True).all()

    today_reservations = Reservation.query.filter(
        Reservation.reservation_date == today,
        Reservation.status.in_(["confirmed", "seated"]),
    ).order_by(Reservation.reservation_time).all()

    upcoming_count = Reservation.query.filter(
        Reservation.reservation_date >= today,
        Reservation.status == "confirmed",
    ).count()

    no_show_count = NoShowRecord.query.filter(
        NoShowRecord.occurred_at >= dt.datetime.utcnow() - dt.timedelta(days=30),
    ).count()

    waitlist_count = WaitlistEntry.query.filter_by(status="waiting").count()

    return render_template("admin/dashboard.html",
                           restaurants=restaurants,
                           today_reservations=today_reservations,
                           upcoming_count=upcoming_count,
                           no_show_count=no_show_count,
                           waitlist_count=waitlist_count,
                           today=today, as_money=as_money)


@app.route("/admin/restaurants")
def admin_restaurants():
    require_admin()
    restaurants = Restaurant.query.order_by(Restaurant.name).all()
    return render_template("admin/restaurants.html", restaurants=restaurants)


@app.route("/admin/restaurant/new", methods=["GET", "POST"])
def admin_restaurant_new():
    require_admin()
    if request.method == "POST":
        r = Restaurant(
            name=request.form["name"],
            slug=make_slug(request.form["name"]),
            description=request.form.get("description", ""),
            cuisine_type=request.form.get("cuisine_type", ""),
            address=request.form.get("address", ""),
            phone=request.form.get("phone", ""),
            email=request.form.get("email", ""),
            image_url=request.form.get("image_url", ""),
            slot_duration_minutes=int(request.form.get("slot_duration_minutes", 90)),
            max_party_size=int(request.form.get("max_party_size", 12)),
            deposit_type=request.form.get("deposit_type", "per_person"),
            deposit_amount_cents=int(float(request.form.get("deposit_amount", 10)) * 100),
            require_deposit=request.form.get("require_deposit") == "on",
            require_card_hold=request.form.get("require_card_hold") == "on",
            cancellation_cutoff_hours=int(request.form.get("cancellation_cutoff_hours", 24)),
            no_show_fee_cents=int(float(request.form.get("no_show_fee", 25)) * 100),
            late_cancel_fee_cents=int(float(request.form.get("late_cancel_fee", 15)) * 100),
        )
        hours_data = {}
        for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            periods = []
            opens = request.form.getlist(f"{day}_open")
            closes = request.form.getlist(f"{day}_close")
            for o, c in zip(opens, closes):
                if o and c:
                    periods.append([o, c])
            hours_data[day] = periods
        r.opening_hours = json.dumps(hours_data)

        db.session.add(r)
        db.session.commit()
        flash("Restaurant created!", "success")
        return redirect(url_for("admin_restaurants"))

    return render_template("admin/restaurant_form.html",
                           restaurant=None, cuisine_types=CUISINE_TYPES)


@app.route("/admin/restaurant/<int:rid>/edit", methods=["GET", "POST"])
def admin_restaurant_edit(rid):
    require_admin()
    r = Restaurant.query.get_or_404(rid)
    if request.method == "POST":
        r.name = request.form["name"]
        r.slug = make_slug(request.form["name"])
        r.description = request.form.get("description", "")
        r.cuisine_type = request.form.get("cuisine_type", "")
        r.address = request.form.get("address", "")
        r.phone = request.form.get("phone", "")
        r.email = request.form.get("email", "")
        r.image_url = request.form.get("image_url", "")
        r.slot_duration_minutes = int(request.form.get("slot_duration_minutes", 90))
        r.max_party_size = int(request.form.get("max_party_size", 12))
        r.deposit_type = request.form.get("deposit_type", "per_person")
        r.deposit_amount_cents = int(float(request.form.get("deposit_amount", 10)) * 100)
        r.require_deposit = request.form.get("require_deposit") == "on"
        r.require_card_hold = request.form.get("require_card_hold") == "on"
        r.cancellation_cutoff_hours = int(request.form.get("cancellation_cutoff_hours", 24))
        r.no_show_fee_cents = int(float(request.form.get("no_show_fee", 25)) * 100)
        r.late_cancel_fee_cents = int(float(request.form.get("late_cancel_fee", 15)) * 100)
        r.active = request.form.get("active") == "on"

        hours_data = {}
        for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            periods = []
            opens = request.form.getlist(f"{day}_open")
            closes = request.form.getlist(f"{day}_close")
            for o, c in zip(opens, closes):
                if o and c:
                    periods.append([o, c])
            hours_data[day] = periods
        r.opening_hours = json.dumps(hours_data)

        db.session.commit()
        flash("Restaurant updated!", "success")
        return redirect(url_for("admin_restaurants"))

    return render_template("admin/restaurant_form.html",
                           restaurant=r, cuisine_types=CUISINE_TYPES)


@app.route("/admin/restaurant/<int:rid>/tables", methods=["GET"])
def admin_tables(rid):
    require_admin()
    restaurant = Restaurant.query.get_or_404(rid)
    tables = Table.query.filter_by(restaurant_id=rid).order_by(Table.name).all()
    return render_template("admin/tables.html", restaurant=restaurant, tables=tables)


@app.route("/admin/restaurant/<int:rid>/table/add", methods=["POST"])
def admin_table_add(rid):
    require_admin()
    table = Table(
        restaurant_id=rid,
        name=request.form["name"],
        capacity=int(request.form.get("capacity", 4)),
        table_type=request.form.get("table_type", "standard"),
    )
    db.session.add(table)
    db.session.commit()
    flash("Table added!", "success")
    return redirect(url_for("admin_tables", rid=rid))


@app.route("/admin/table/<int:tid>/edit", methods=["POST"])
def admin_table_edit(tid):
    require_admin()
    table = Table.query.get_or_404(tid)
    table.name = request.form.get("name", table.name)
    table.capacity = int(request.form.get("capacity", table.capacity))
    table.table_type = request.form.get("table_type", table.table_type)
    table.active = request.form.get("active") == "on"
    db.session.commit()
    flash("Table updated!", "success")
    return redirect(url_for("admin_tables", rid=table.restaurant_id))


@app.route("/admin/table/<int:tid>/delete", methods=["POST"])
def admin_table_delete(tid):
    require_admin()
    table = Table.query.get_or_404(tid)
    rid = table.restaurant_id
    db.session.delete(table)
    db.session.commit()
    flash("Table deleted.", "success")
    return redirect(url_for("admin_tables", rid=rid))


@app.route("/admin/restaurant/<int:rid>/reservations")
def admin_reservations(rid):
    require_admin()
    restaurant = Restaurant.query.get_or_404(rid)
    date_filter = request.args.get("date", dt.date.today().isoformat())
    status_filter = request.args.get("status", "all")

    try:
        filter_date = dt.date.fromisoformat(date_filter)
    except:
        filter_date = dt.date.today()

    query = Reservation.query.filter_by(restaurant_id=rid)

    if date_filter != "all":
        query = query.filter(Reservation.reservation_date == filter_date)
    if status_filter != "all":
        query = query.filter(Reservation.status == status_filter)

    reservations = query.order_by(Reservation.reservation_time).all()

    return render_template("admin/reservations.html",
                           restaurant=restaurant,
                           reservations=reservations,
                           filter_date=filter_date,
                           status_filter=status_filter,
                           as_money=as_money)


@app.route("/admin/reservation/<int:res_id>/status", methods=["POST"])
def admin_update_status(res_id):
    require_admin()
    reservation = Reservation.query.get_or_404(res_id)
    new_status = request.form.get("status")

    if new_status == "seated":
        reservation.status = "seated"
    elif new_status == "completed":
        reservation.status = "completed"
    elif new_status == "no_show":
        reservation.status = "no_show"
        record = NoShowRecord(
            guest_email=reservation.guest_email,
            guest_phone=reservation.guest_phone,
            restaurant_id=reservation.restaurant_id,
            reservation_id=reservation.id,
        )

        fee_amount = reservation.restaurant.no_show_fee_cents
        if reservation.stripe_payment_method_id and stripe.api_key and fee_amount > 0:
            try:
                stripe.PaymentIntent.create(
                    amount=fee_amount,
                    currency="usd",
                    payment_method=reservation.stripe_payment_method_id,
                    confirm=True,
                    automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                    metadata={"type": "no_show_fee",
                              "reservation_uuid": reservation.uuid}
                )
                reservation.no_show_fee_charged = True
                reservation.no_show_fee_amount_cents = fee_amount
                record.fee_charged_cents = fee_amount
            except Exception as e:
                print(f"No-show charge error: {e}")

        db.session.add(record)

        try:
            send_no_show_email(reservation, fee_amount)
        except:
            pass

        notify_waitlist_for_slot(reservation.restaurant_id, reservation.reservation_date,
                                 reservation.reservation_time, reservation.party_size)

    elif new_status == "cancelled":
        reservation.status = "cancelled"
        reservation.cancelled_at = dt.datetime.utcnow()
        notify_waitlist_for_slot(reservation.restaurant_id, reservation.reservation_date,
                                 reservation.reservation_time, reservation.party_size)

    db.session.commit()
    flash(f"Reservation status updated to {new_status}.", "success")
    return redirect(url_for("admin_reservations", rid=reservation.restaurant_id,
                            date=reservation.reservation_date.isoformat()))


@app.route("/admin/no-show-stats")
def admin_no_show_stats():
    require_admin()
    days = request.args.get("days", 30, type=int)
    since = dt.datetime.utcnow() - dt.timedelta(days=days)

    records = db.session.query(
        NoShowRecord.guest_email,
        db.func.count(NoShowRecord.id).label("count"),
        db.func.sum(NoShowRecord.fee_charged_cents).label("total_fees"),
    ).filter(NoShowRecord.occurred_at >= since)\
     .group_by(NoShowRecord.guest_email)\
     .order_by(db.desc("count")).all()

    restaurant_stats = db.session.query(
        Restaurant.name,
        db.func.count(NoShowRecord.id).label("count"),
    ).join(NoShowRecord).filter(NoShowRecord.occurred_at >= since)\
     .group_by(Restaurant.name).all()

    total_no_shows = sum(r.count for r in records)
    total_fees = sum((r.total_fees or 0) for r in records)

    return render_template("admin/no_show_stats.html",
                           records=records, restaurant_stats=restaurant_stats,
                           total_no_shows=total_no_shows, total_fees=total_fees,
                           days=days, as_money=as_money)


@app.route("/admin/promo-codes")
def admin_promo_codes():
    require_admin()
    codes = PromoCode.query.order_by(PromoCode.created_at.desc()).all()
    restaurants = Restaurant.query.filter_by(active=True).all()
    return render_template("admin/promo_codes.html", codes=codes, restaurants=restaurants,
                           as_money=as_money)


@app.route("/admin/promo-code/add", methods=["POST"])
def admin_promo_code_add():
    require_admin()
    code = PromoCode(
        code=request.form["code"].strip().upper(),
        discount_type=request.form.get("discount_type", "percent"),
        discount_value=int(request.form.get("discount_value", 10)),
        restaurant_id=int(request.form["restaurant_id"]) if request.form.get("restaurant_id") else None,
        waive_deposit=request.form.get("waive_deposit") == "on",
        active=True,
        max_uses=int(request.form["max_uses"]) if request.form.get("max_uses") else None,
    )
    if request.form.get("valid_from"):
        code.valid_from = dt.date.fromisoformat(request.form["valid_from"])
    if request.form.get("valid_to"):
        code.valid_to = dt.date.fromisoformat(request.form["valid_to"])
    db.session.add(code)
    db.session.commit()
    flash("Promo code created!", "success")
    return redirect(url_for("admin_promo_codes"))


@app.route("/admin/promo-code/<int:pid>/toggle", methods=["POST"])
def admin_promo_code_toggle(pid):
    require_admin()
    code = PromoCode.query.get_or_404(pid)
    code.active = not code.active
    db.session.commit()
    flash(f"Promo code {'activated' if code.active else 'deactivated'}.", "success")
    return redirect(url_for("admin_promo_codes"))


@app.route("/admin/waitlist")
def admin_waitlist():
    require_admin()
    entries = WaitlistEntry.query.filter(
        WaitlistEntry.status.in_(["waiting", "notified"])
    ).order_by(WaitlistEntry.desired_date, WaitlistEntry.created_at).all()
    return render_template("admin/waitlist.html", entries=entries, as_money=as_money)


# ──────────────── API Routes ────────────────

@app.route("/api/availability", methods=["POST"])
def api_availability():
    data = request.get_json()
    restaurant_id = data.get("restaurant_id")
    date_str = data.get("date")
    party_size = data.get("party_size", 2)

    restaurant = Restaurant.query.get(restaurant_id)
    if not restaurant:
        return jsonify({"error": "Restaurant not found"}), 404

    try:
        check_date = dt.date.fromisoformat(date_str)
    except:
        return jsonify({"error": "Invalid date"}), 400

    day_key = get_day_key(check_date)
    hours = restaurant.get_opening_hours()
    day_hours = hours.get(day_key, [])

    slots = []
    if day_hours:
        all_slots = generate_time_slots(day_hours)
        now = dt.datetime.now()
        for slot_str in all_slots:
            slot_time = dt.datetime.strptime(slot_str, "%H:%M").time()
            if check_date == dt.date.today() and slot_time <= now.time():
                continue
            end_time = calculate_end_time(slot_time, restaurant.slot_duration_minutes)
            tables = find_available_tables(restaurant.id, check_date, slot_time, end_time, party_size)
            slots.append({
                "time": slot_str,
                "time_display": dt.datetime.strptime(slot_str, "%H:%M").strftime("%-I:%M %p"),
                "available": len(tables) > 0,
                "tables_left": len(tables)
            })

    return jsonify({"slots": slots, "date": date_str, "party_size": party_size})


# ──────────────── Run ────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
