import datetime as dt
import json
import uuid
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import URLSafeTimedSerializer
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

serializer = None

def init_serializer(app_secret):
    global serializer
    serializer = URLSafeTimedSerializer(app_secret)


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

    stripe_connect_id = db.Column(db.String(200))
    stripe_connect_status = db.Column(db.String(50), default="not_connected")
    stripe_charges_enabled = db.Column(db.Boolean, default=False)
    stripe_payouts_enabled = db.Column(db.Boolean, default=False)
    platform_fee_percent = db.Column(db.Integer, default=10)

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


class RestaurantUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(50))
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurant.id"), nullable=True)
    approved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    restaurant = db.relationship("Restaurant", backref="owner_account")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class PaymentTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"), nullable=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurant.id"), nullable=False)
    transaction_type = db.Column(db.String(50), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    platform_fee_cents = db.Column(db.Integer, default=0)
    restaurant_amount_cents = db.Column(db.Integer, default=0)
    stripe_payment_intent_id = db.Column(db.String(200))
    stripe_transfer_id = db.Column(db.String(200))
    status = db.Column(db.String(30), default="pending")
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    reservation = db.relationship("Reservation", backref="transactions")
    restaurant = db.relationship("Restaurant", backref="transactions")


class RestaurantNomination(db.Model):
    __tablename__ = "restaurant_nomination"
    id = db.Column(db.Integer, primary_key=True)
    restaurant_name = db.Column(db.String(200), nullable=False)
    city = db.Column(db.String(200), nullable=False)
    restaurant_email = db.Column(db.String(200))
    nominator_name = db.Column(db.String(200))
    nominator_email = db.Column(db.String(200))
    status = db.Column(db.String(30), default="pending")
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
