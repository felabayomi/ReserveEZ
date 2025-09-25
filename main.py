import os, datetime as dt, io, json
from decimal import Decimal
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, send_file, flash
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import qrcode
import requests
import stripe
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content

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

# SendGrid configuration
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")

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
    purchase_email_sent = db.Column(db.Boolean, default=False)
    activation_email_sent = db.Column(db.Boolean, default=False)
    expiration_warning_sent = db.Column(db.Boolean, default=False)

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
    reminder_24h_sent = db.Column(db.Boolean, default=False)
    reminder_2h_sent = db.Column(db.Boolean, default=False)

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

def send_booking_confirmation_email(booking):
    """Send booking confirmation email to user and admin using SendGrid"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping email notification")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        # Prepare email content
        subject = f"Booking Confirmation - {booking.resource.name}"
        
        # Create HTML content with booking details
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                <h2 style="color: #28a745; margin: 0;">Booking Confirmed!</h2>
                <p style="margin: 10px 0 0 0;">Thank you for choosing EasyDesk at City Discoverer</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Booking Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Workspace:</td>
                        <td style="padding: 8px 0;">{booking.resource.name}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Customer:</td>
                        <td style="padding: 8px 0;">{booking.customer_email}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Date & Time:</td>
                        <td style="padding: 8px 0;">{booking.start_dt.strftime('%B %d, %Y at %I:%M %p')} - {booking.end_dt.strftime('%I:%M %p')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Duration:</td>
                        <td style="padding: 8px 0;">{int((booking.end_dt - booking.start_dt).total_seconds() / 3600)} hours</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Seats:</td>
                        <td style="padding: 8px 0;">{booking.num_seats}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Total:</td>
                        <td style="padding: 8px 0; font-weight: bold; color: #28a745;">{as_money(booking.total_cost_cents)}</td>
                    </tr>
                </table>
            </div>
            
            <div style="background-color: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <p style="margin: 0; font-size: 14px; color: #6c757d;">
                    <strong>Location:</strong> City Discoverer<br>
                    50 Stately St, Suite 2, Wiley Ford WV 26767
                </p>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Questions? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Send separate emails to customer and admin for better deliverability
        # Customer email
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(booking.customer_email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Admin email with [ADMIN COPY] prefix
        admin_subject = f"[ADMIN COPY] {subject}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"Customer email sent! Status: {customer_response.status_code}")
        print(f"Admin email sent! Status: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send booking confirmation email: {str(e)}")
        return False

def send_test_emails(test_emails):
    """Send test emails with sample booking data"""
    if not SENDGRID_API_KEY:
        return {"error": "SendGrid API key not configured"}
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        # Create mock booking data for the test
        class MockResource:
            name = "Hot Desk #5"
        
        class MockBooking:
            resource = MockResource()
            customer_email = test_emails[0]  # Use first test email as customer
            start_dt = dt.datetime(2025, 9, 26, 9, 0)  # Tomorrow at 9 AM
            end_dt = dt.datetime(2025, 9, 26, 12, 0)   # Until 12 PM
            num_seats = 2
            total_cost_cents = 1500  # $15.00
        
        mock_booking = MockBooking()
        
        # Prepare email content
        subject = f"[TEST] Booking Confirmation - {mock_booking.resource.name}"
        
        # Create HTML content with mock booking details
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="color: #856404; margin: 0;">⚡ TEST EMAIL</h4>
                <p style="margin: 5px 0 0 0; color: #856404;">This is a test of your booking confirmation email template.</p>
            </div>
            
            <div style="background-color: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                <h2 style="color: #28a745; margin: 0;">Booking Confirmed!</h2>
                <p style="margin: 10px 0 0 0;">Thank you for choosing EasyDesk at City Discoverer</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Booking Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Workspace:</td>
                        <td style="padding: 8px 0;">{mock_booking.resource.name}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Customer:</td>
                        <td style="padding: 8px 0;">{mock_booking.customer_email}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Date & Time:</td>
                        <td style="padding: 8px 0;">{mock_booking.start_dt.strftime('%B %d, %Y at %I:%M %p')} - {mock_booking.end_dt.strftime('%I:%M %p')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Duration:</td>
                        <td style="padding: 8px 0;">{int((mock_booking.end_dt - mock_booking.start_dt).total_seconds() / 3600)} hours</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Seats:</td>
                        <td style="padding: 8px 0;">{mock_booking.num_seats}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Total:</td>
                        <td style="padding: 8px 0; font-weight: bold; color: #28a745;">{as_money(mock_booking.total_cost_cents)}</td>
                    </tr>
                </table>
            </div>
            
            <div style="background-color: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <p style="margin: 0; font-size: 14px; color: #6c757d;">
                    <strong>Location:</strong> City Discoverer<br>
                    50 Stately St, Suite 2, Wiley Ford WV 26767
                </p>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Questions? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Create separate emails for customer and admin to ensure both are delivered
        # Send to customer first
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(test_emails[0]),  # Customer email
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send to admin
        admin_subject = f"[ADMIN COPY] {subject}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),  # Admin email
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send customer email
        customer_response = sg.send(customer_message)
        print(f"Customer test email sent! Status code: {customer_response.status_code}")
        
        # Send admin email
        admin_response = sg.send(admin_message)
        print(f"Admin test email sent! Status code: {admin_response.status_code}")
        
        return {"success": True, "customer_status": customer_response.status_code, "admin_status": admin_response.status_code, "recipients": test_emails + ["hello@citydiscoverer.ai"]}
        
    except Exception as e:
        print(f"Failed to send test emails: {str(e)}")
        return {"error": str(e)}

def send_booking_reminder(booking, hours_before):
    """Send booking reminder email to customer and admin"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping reminder email")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        # Determine reminder type and message
        if hours_before == 24:
            reminder_type = "24-Hour"
            time_text = "tomorrow"
            urgency_color = "#17a2b8"  # Blue
            icon = "📅"
        else:  # 2 hours
            reminder_type = "2-Hour"
            time_text = "in 2 hours"
            urgency_color = "#fd7e14"  # Orange
            icon = "⏰"
        
        subject = f"{reminder_type} Reminder: Your {booking.resource.name} booking starts {time_text}"
        
        # Create HTML content for reminder
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: {urgency_color}; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">{icon} Booking Reminder</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">Your workspace is reserved {time_text}!</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Booking Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Workspace:</td>
                        <td style="padding: 8px 0;">{booking.resource.name}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Customer:</td>
                        <td style="padding: 8px 0;">{booking.email}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Date & Time:</td>
                        <td style="padding: 8px 0;">{booking.start_dt.strftime('%B %d, %Y at %I:%M %p')} - {booking.end_dt.strftime('%I:%M %p')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Duration:</td>
                        <td style="padding: 8px 0;">{int(booking.hours)} hours</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Seats:</td>
                        <td style="padding: 8px 0;">{booking.seats}</td>
                    </tr>
                </table>
            </div>
            
            <div style="background-color: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <p style="margin: 0; font-size: 14px; color: #6c757d;">
                    <strong>Location:</strong> City Discoverer<br>
                    50 Stately St, Suite 2, Wiley Ford WV 26767
                </p>
            </div>
            
            <div style="background-color: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #155724;">💡 Getting Ready?</h4>
                <ul style="margin: 0; padding-left: 20px; color: #155724;">
                    <li>Arrive 5-10 minutes early for check-in</li>
                    <li>Bring your laptop and any work materials</li>
                    <li>Free WiFi and power outlets available</li>
                    <li>Questions? Reply to this email</li>
                </ul>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Need to cancel or modify? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Send to customer
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(booking.email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send to admin with prefix
        admin_subject = f"[ADMIN] {subject}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"{reminder_type} reminder sent! Customer: {customer_response.status_code}, Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        reminder_desc = "24-hour" if hours_before == 24 else "2-hour"
        print(f"Failed to send {reminder_desc} reminder: {str(e)}")
        return False

def check_and_send_reminders():
    """Check for upcoming bookings and send reminders"""
    try:
        now = dt.datetime.utcnow()
        
        # Check for 24-hour reminders
        reminder_24h_time = now + dt.timedelta(hours=24)
        bookings_24h = Booking.query.filter(
            Booking.status.in_(["confirmed", "paid", "free"]),
            Booking.start_dt.between(now + dt.timedelta(hours=23), now + dt.timedelta(hours=25)),
            Booking.reminder_24h_sent == False
        ).all()
        
        for booking in bookings_24h:
            if send_booking_reminder(booking, 24):
                booking.reminder_24h_sent = True
                db.session.commit()
        
        # Check for 2-hour reminders
        bookings_2h = Booking.query.filter(
            Booking.status.in_(["confirmed", "paid", "free"]),
            Booking.start_dt.between(now + dt.timedelta(hours=1, minutes=30), now + dt.timedelta(hours=2, minutes=30)),
            Booking.reminder_2h_sent == False
        ).all()
        
        for booking in bookings_2h:
            if send_booking_reminder(booking, 2):
                booking.reminder_2h_sent = True
                db.session.commit()
        
        total_sent = len(bookings_24h) + len(bookings_2h)
        if total_sent > 0:
            print(f"Sent {total_sent} reminder emails: {len(bookings_24h)} 24h, {len(bookings_2h)} 2h")
        
        return {"sent_24h": len(bookings_24h), "sent_2h": len(bookings_2h)}
        
    except Exception as e:
        print(f"Error checking reminders: {str(e)}")
        return {"error": str(e)}

def send_pass_purchase_confirmation(pass_obj):
    """Send pass purchase confirmation email"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping pass confirmation email")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        # Determine pass details
        pass_type_display = pass_obj.pass_type.title()
        pass_icon = {"day": "📅", "week": "📆", "month": "🗓️"}.get(pass_obj.pass_type, "🎫")
        
        # Calculate pass value and duration
        duration_text = {"day": "1 day", "week": "7 days", "month": "30 days"}[pass_obj.pass_type]
        
        subject = f"{pass_type_display} Pass Purchased - Unlimited Bookings!"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #28a745; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">{pass_icon} Pass Purchase Confirmed!</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">Your {pass_type_display} Pass is ready to use</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Pass Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Pass Type:</td>
                        <td style="padding: 8px 0;">{pass_type_display} Pass</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Email:</td>
                        <td style="padding: 8px 0;">{pass_obj.email}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Valid From:</td>
                        <td style="padding: 8px 0;">{pass_obj.valid_from.strftime('%B %d, %Y at %I:%M %p')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Valid Until:</td>
                        <td style="padding: 8px 0;">{pass_obj.valid_to.strftime('%B %d, %Y at %I:%M %p')}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Duration:</td>
                        <td style="padding: 8px 0;">{duration_text}</td>
                    </tr>
                </table>
            </div>
            
            <div style="background-color: #d1ecf1; border: 1px solid #bee5eb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #0c5460;">🚀 How to Use Your Pass</h4>
                <ul style="margin: 0; padding-left: 20px; color: #0c5460;">
                    <li>Book any workspace during your pass validity period</li>
                    <li>No booking fees during pass period</li>
                    <li>Cancel and rebook freely within hours</li>
                    <li>Pass activates automatically at start time</li>
                </ul>
            </div>
            
            <div style="background-color: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <p style="margin: 0; font-size: 14px; color: #6c757d;">
                    <strong>Location:</strong> City Discoverer<br>
                    50 Stately St, Suite 2, Wiley Ford WV 26767
                </p>
            </div>
            
            <div style="text-align: center; margin: 20px 0;">
                <a href="#" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Start Booking Now</a>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Questions about your pass? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Send to customer
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(pass_obj.email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send to admin
        admin_subject = f"[ADMIN] {subject}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"Pass purchase confirmation sent! Customer: {customer_response.status_code}, Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send pass purchase confirmation: {str(e)}")
        return False

def send_pass_expiration_warning(pass_obj):
    """Send pass expiration warning email (2 days before expiry)"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping expiration warning")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        pass_type_display = pass_obj.pass_type.title()
        pass_icon = {"day": "📅", "week": "📆", "month": "🗓️"}.get(pass_obj.pass_type, "🎫")
        
        subject = f"⚠️ Your {pass_type_display} Pass Expires in 2 Days"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #ffc107; color: #212529; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">⚠️ Pass Expiring Soon</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">Your {pass_type_display} Pass expires in 2 days</p>
            </div>
            
            <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #856404;">🕒 Time Remaining</h4>
                <p style="margin: 0; color: #856404;">
                    Your pass expires on <strong>{pass_obj.valid_to.strftime('%B %d, %Y at %I:%M %p')}</strong>
                </p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Pass Information</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Pass Type:</td>
                        <td style="padding: 8px 0;">{pass_icon} {pass_type_display} Pass</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Status:</td>
                        <td style="padding: 8px 0;">Active</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Expires:</td>
                        <td style="padding: 8px 0; color: #dc3545; font-weight: bold;">{pass_obj.valid_to.strftime('%B %d, %Y at %I:%M %p')}</td>
                    </tr>
                </table>
            </div>
            
            <div style="background-color: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #155724;">💡 Make the Most of Your Pass</h4>
                <ul style="margin: 0; padding-left: 20px; color: #155724;">
                    <li>Book your remaining sessions now</li>
                    <li>No booking fees until expiry</li>
                    <li>Consider purchasing a new pass before this one expires</li>
                </ul>
            </div>
            
            <div style="text-align: center; margin: 20px 0;">
                <a href="#" style="background-color: #28a745; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; margin-right: 10px;">Book Now</a>
                <a href="#" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Renew Pass</a>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Questions? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Send to customer
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(pass_obj.email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send to admin
        admin_subject = f"[ADMIN] {subject}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"Pass expiration warning sent! Customer: {customer_response.status_code}, Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send pass expiration warning: {str(e)}")
        return False

def check_and_send_pass_notifications():
    """Check for pass purchase confirmations and expiration warnings"""
    try:
        # Check for passes that need purchase confirmation
        pending_confirmations = Pass.query.filter(
            Pass.purchase_email_sent == False,
            Pass.status == "active"
        ).all()
        
        for pass_obj in pending_confirmations:
            if send_pass_purchase_confirmation(pass_obj):
                pass_obj.purchase_email_sent = True
                db.session.commit()
        
        # Check for passes expiring in 2 days
        two_days_from_now = dt.datetime.utcnow() + dt.timedelta(days=2)
        one_day_from_now = dt.datetime.utcnow() + dt.timedelta(days=1)
        
        expiring_passes = Pass.query.filter(
            Pass.status == "active",
            Pass.valid_to.between(one_day_from_now, two_days_from_now),
            Pass.expiration_warning_sent == False
        ).all()
        
        for pass_obj in expiring_passes:
            if send_pass_expiration_warning(pass_obj):
                pass_obj.expiration_warning_sent = True
                db.session.commit()
        
        total_sent = len(pending_confirmations) + len(expiring_passes)
        if total_sent > 0:
            print(f"Sent {total_sent} pass emails: {len(pending_confirmations)} confirmations, {len(expiring_passes)} warnings")
        
        return {"sent_confirmations": len(pending_confirmations), "sent_warnings": len(expiring_passes)}
        
    except Exception as e:
        print(f"Error checking pass notifications: {str(e)}")
        return {"error": str(e)}

def send_payment_failure_email(booking_or_pass, error_details=""):
    """Send payment failure notification email"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping payment failure email")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        # Determine if it's a booking or pass
        is_booking = hasattr(booking_or_pass, 'resource')
        if is_booking:
            email = booking_or_pass.email
            item_name = booking_or_pass.resource.name
            item_type = "booking"
            amount = booking_or_pass.amount_cents
        else:  # Pass
            email = booking_or_pass.email
            item_name = f"{booking_or_pass.pass_type.title()} Pass"
            item_type = "pass"
            # Get amount from payment record
            payment = Payment.query.filter_by(pass_id=booking_or_pass.id).first()
            amount = payment.amount_cents if payment else 0
        
        subject = f"Payment Issue - {item_name}"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #dc3545; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">⚠️ Payment Issue</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">There was a problem processing your payment</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">{item_type.title()} Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">{item_type.title()}:</td>
                        <td style="padding: 8px 0;">{item_name}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Email:</td>
                        <td style="padding: 8px 0;">{email}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Amount:</td>
                        <td style="padding: 8px 0; color: #dc3545; font-weight: bold;">{as_money(amount)}</td>
                    </tr>
                </table>
            </div>
            
            <div style="background-color: #f8d7da; border: 1px solid #f5c6cb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #721c24;">💳 What happened?</h4>
                <p style="margin: 0; color: #721c24;">
                    Your payment could not be processed. This might be due to insufficient funds, expired card, or payment method restrictions.
                    {f' Error details: {error_details}' if error_details else ''}
                </p>
            </div>
            
            <div style="background-color: #d1ecf1; border: 1px solid #bee5eb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #0c5460;">🔧 Next Steps</h4>
                <ul style="margin: 0; padding-left: 20px; color: #0c5460;">
                    <li>Check your payment method is valid and has sufficient funds</li>
                    <li>Try a different payment method</li>
                    <li>Contact your bank if the issue persists</li>
                    <li>Reply to this email for assistance</li>
                </ul>
            </div>
            
            <div style="text-align: center; margin: 20px 0;">
                <a href="#" style="background-color: #28a745; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Try Payment Again</a>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Need help? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Send to customer
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send to admin
        admin_subject = f"[ADMIN] Payment Failure - {item_name}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"Payment failure email sent! Customer: {customer_response.status_code}, Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send payment failure email: {str(e)}")
        return False

def send_refund_confirmation_email(booking_or_pass, refund_amount_cents, refund_reason=""):
    """Send refund confirmation email"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping refund confirmation email")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        # Determine if it's a booking or pass
        is_booking = hasattr(booking_or_pass, 'resource')
        if is_booking:
            email = booking_or_pass.email
            item_name = booking_or_pass.resource.name
            item_type = "booking"
            if hasattr(booking_or_pass, 'start_dt'):
                item_details = f"on {booking_or_pass.start_dt.strftime('%B %d, %Y')}"
            else:
                item_details = ""
        else:  # Pass
            email = booking_or_pass.email
            item_name = f"{booking_or_pass.pass_type.title()} Pass"
            item_type = "pass"
            item_details = f"valid until {booking_or_pass.valid_to.strftime('%B %d, %Y')}"
        
        subject = f"Refund Confirmed - {as_money(refund_amount_cents)} for {item_name}"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #28a745; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">✅ Refund Processed</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">Your refund of {as_money(refund_amount_cents)} has been confirmed</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Refund Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">{item_type.title()}:</td>
                        <td style="padding: 8px 0;">{item_name} {item_details}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Email:</td>
                        <td style="padding: 8px 0;">{email}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Refund Amount:</td>
                        <td style="padding: 8px 0; color: #28a745; font-weight: bold; font-size: 18px;">{as_money(refund_amount_cents)}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Refund Date:</td>
                        <td style="padding: 8px 0;">{dt.datetime.utcnow().strftime('%B %d, %Y at %I:%M %p')}</td>
                    </tr>
                </table>
            </div>
            
            {f'''<div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #856404;">📝 Refund Reason</h4>
                <p style="margin: 0; color: #856404;">{refund_reason}</p>
            </div>''' if refund_reason else ''}
            
            <div style="background-color: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #155724;">💳 Payment Information</h4>
                <ul style="margin: 0; padding-left: 20px; color: #155724;">
                    <li>Refund will appear on your original payment method</li>
                    <li>Processing time: 3-5 business days</li>
                    <li>You will receive a separate notification from your bank/card issuer</li>
                    <li>Questions? Contact us anytime</li>
                </ul>
            </div>
            
            <div style="background-color: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <p style="margin: 0; font-size: 14px; color: #6c757d;">
                    <strong>Still need workspace?</strong><br>
                    City Discoverer - 50 Stately St, Suite 2, Wiley Ford WV 26767
                </p>
            </div>
            
            <div style="text-align: center; margin: 20px 0;">
                <a href="#" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Book Again</a>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Questions about your refund? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Send to customer
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send to admin
        admin_subject = f"[ADMIN] Refund Processed - {as_money(refund_amount_cents)} for {item_name}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"Refund confirmation email sent! Customer: {customer_response.status_code}, Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send refund confirmation email: {str(e)}")
        return False

def send_booking_cancellation_email(booking, cancellation_reason="", refund_amount_cents=0):
    """Send booking cancellation confirmation email"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping cancellation email")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        subject = f"Booking Cancelled - {booking.resource.name}"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #ffc107; color: #212529; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">📅 Booking Cancelled</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">Your booking has been successfully cancelled</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Cancelled Booking Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Workspace:</td>
                        <td style="padding: 8px 0;">{booking.resource.name}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Email:</td>
                        <td style="padding: 8px 0;">{booking.email}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Original Date:</td>
                        <td style="padding: 8px 0;">{booking.start_dt.strftime('%B %d, %Y')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Original Time:</td>
                        <td style="padding: 8px 0;">{booking.start_dt.strftime('%I:%M %p')} - {booking.end_dt.strftime('%I:%M %p')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Duration:</td>
                        <td style="padding: 8px 0;">{booking.duration_hours} hour{"s" if booking.duration_hours > 1 else ""}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Status:</td>
                        <td style="padding: 8px 0; color: #dc3545; font-weight: bold;">CANCELLED</td>
                    </tr>
                </table>
            </div>
            
            {f'''<div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #856404;">📝 Cancellation Reason</h4>
                <p style="margin: 0; color: #856404;">{cancellation_reason}</p>
            </div>''' if cancellation_reason else ''}
            
            {f'''<div style="background-color: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #155724;">💰 Refund Information</h4>
                <p style="margin: 0; color: #155724;">
                    A refund of <strong>{as_money(refund_amount_cents)}</strong> will be processed to your original payment method within 3-5 business days.
                </p>
            </div>''' if refund_amount_cents > 0 else ''}
            
            <div style="background-color: #d1ecf1; border: 1px solid #bee5eb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #0c5460;">🔄 Next Steps</h4>
                <ul style="margin: 0; padding-left: 20px; color: #0c5460;">
                    <li>Your booking slot is now available for other customers</li>
                    <li>You can book again anytime at your convenience</li>
                    <li>Consider our passes for unlimited bookings</li>
                    <li>Questions? Reply to this email</li>
                </ul>
            </div>
            
            <div style="background-color: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <p style="margin: 0; font-size: 14px; color: #6c757d;">
                    <strong>Location:</strong> City Discoverer<br>
                    50 Stately St, Suite 2, Wiley Ford WV 26767
                </p>
            </div>
            
            <div style="text-align: center; margin: 20px 0;">
                <a href="#" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; margin-right: 10px;">Book Again</a>
                <a href="#" style="background-color: #28a745; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">View Passes</a>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Questions about your cancellation? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Send to customer
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(booking.email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send to admin
        admin_subject = f"[ADMIN] Booking Cancelled - {booking.resource.name}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"Booking cancellation email sent! Customer: {customer_response.status_code}, Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send booking cancellation email: {str(e)}")
        return False

def send_booking_modification_email(booking, original_booking_data, modification_reason=""):
    """Send booking modification confirmation email"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping modification email")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        subject = f"Booking Modified - {booking.resource.name}"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #17a2b8; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">📝 Booking Modified</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">Your booking details have been updated</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Updated Booking Details</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Workspace:</td>
                        <td style="padding: 8px 0;">{booking.resource.name}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Email:</td>
                        <td style="padding: 8px 0;">{booking.email}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">New Date:</td>
                        <td style="padding: 8px 0; color: #28a745; font-weight: bold;">{booking.start_dt.strftime('%B %d, %Y')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">New Time:</td>
                        <td style="padding: 8px 0; color: #28a745; font-weight: bold;">{booking.start_dt.strftime('%I:%M %p')} - {booking.end_dt.strftime('%I:%M %p')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Duration:</td>
                        <td style="padding: 8px 0;">{booking.duration_hours} hour{"s" if booking.duration_hours > 1 else ""}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; font-weight: bold; color: #6c757d;">Status:</td>
                        <td style="padding: 8px 0; color: #28a745; font-weight: bold;">CONFIRMED</td>
                    </tr>
                </table>
            </div>
            
            {f'''<div style="background-color: #f8f9fa; border: 1px solid #dee2e6; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #495057;">📅 Original Booking (Changed From)</h4>
                <table style="width: 100%; font-size: 14px;">
                    <tr>
                        <td style="padding: 4px 0; color: #6c757d;">Date:</td>
                        <td style="padding: 4px 0; text-decoration: line-through;">{original_booking_data.get('date', 'N/A')}</td>
                    </tr>
                    <tr>
                        <td style="padding: 4px 0; color: #6c757d;">Time:</td>
                        <td style="padding: 4px 0; text-decoration: line-through;">{original_booking_data.get('time', 'N/A')}</td>
                    </tr>
                </table>
            </div>''' if original_booking_data else ''}
            
            {f'''<div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #856404;">📝 Modification Reason</h4>
                <p style="margin: 0; color: #856404;">{modification_reason}</p>
            </div>''' if modification_reason else ''}
            
            <div style="background-color: #d1ecf1; border: 1px solid #bee5eb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #0c5460;">✅ What's Next</h4>
                <ul style="margin: 0; padding-left: 20px; color: #0c5460;">
                    <li>Your new booking time is confirmed and reserved</li>
                    <li>Save this email as your booking confirmation</li>
                    <li>Arrive 5 minutes early for check-in</li>
                    <li>You'll receive reminder emails before your session</li>
                </ul>
            </div>
            
            <div style="background-color: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <p style="margin: 0; font-size: 14px; color: #6c757d;">
                    <strong>Location:</strong> City Discoverer<br>
                    50 Stately St, Suite 2, Wiley Ford WV 26767
                </p>
            </div>
            
            <div style="text-align: center; margin: 20px 0;">
                <a href="#" style="background-color: #28a745; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Add to Calendar</a>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Questions about your modified booking? Reply to this email or contact us at hello@citydiscoverer.ai</p>
            </div>
        </body>
        </html>
        """
        
        # Send to customer
        customer_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To(booking.email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send to admin
        admin_subject = f"[ADMIN] Booking Modified - {booking.resource.name}"
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk Booking System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"Booking modification email sent! Customer: {customer_response.status_code}, Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send booking modification email: {str(e)}")
        return False

def send_welcome_email(email, customer_name=""):
    """Send welcome email to new customers"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping welcome email")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        name_display = customer_name if customer_name else "there"
        subject = f"Welcome to City Discoverer EasyDesk! 🎉"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #6f42c1; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">🎉 Welcome to EasyDesk!</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">We're excited to have you join our coworking community</p>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">Hi {name_display}! 👋</h3>
                <p style="margin: 0 0 15px 0; line-height: 1.6;">
                    Thank you for choosing City Discoverer's EasyDesk booking system! We're thrilled to welcome you to our flexible workspace community.
                </p>
                <p style="margin: 0; line-height: 1.6;">
                    Whether you're a remote worker, entrepreneur, student, or just need a change of scenery, our workspace is designed to help you be productive and comfortable.
                </p>
            </div>
            
            <div style="background-color: #e7f3ff; border: 1px solid #bee5eb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #0c5460;">🏢 About Our Space</h4>
                <ul style="margin: 0; padding-left: 20px; color: #0c5460;">
                    <li>Professional workspace with high-speed WiFi</li>
                    <li>Comfortable seating and work stations</li>
                    <li>Quiet environment perfect for focus</li>
                    <li>Meeting rooms and collaboration spaces</li>
                </ul>
            </div>
            
            <div style="background-color: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #155724;">🚀 Getting Started</h4>
                <ul style="margin: 0; padding-left: 20px; color: #155724;">
                    <li>Book by the hour or purchase a day/week/month pass</li>
                    <li>Use promo code <strong>EASYWEEK</strong> for 100% off your first booking!</li>
                    <li>Passes give unlimited bookings during valid periods</li>
                    <li>Cancel and reschedule easily within our system</li>
                </ul>
            </div>
            
            <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #856404;">📍 Location & Hours</h4>
                <p style="margin: 0; color: #856404;">
                    <strong>City Discoverer</strong><br>
                    50 Stately St, Suite 2<br>
                    Wiley Ford, WV 26767<br><br>
                    <strong>Hours:</strong> Monday - Friday, 9 AM - 5 PM<br>
                    <strong>Weekend hours:</strong> Available by appointment
                </p>
            </div>
            
            <div style="background-color: #f8f9fa; border: 1px solid #dee2e6; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h4 style="margin: 0 0 10px 0; color: #495057;">💡 Pro Tips</h4>
                <ul style="margin: 0; padding-left: 20px; color: #495057;">
                    <li>Book in advance for peak times (mornings and mid-week)</li>
                    <li>Bring headphones for calls and video conferences</li>
                    <li>Check in with us when you arrive for the best experience</li>
                    <li>Join our community for networking opportunities</li>
                </ul>
            </div>
            
            <div style="text-align: center; margin: 20px 0;">
                <a href="#" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; margin-right: 10px;">Book Your First Session</a>
                <a href="#" style="background-color: #28a745; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Explore Passes</a>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Questions or need help getting started? Reply to this email or contact us at hello@citydiscoverer.ai<br>
                We're here to help make your workspace experience amazing!</p>
            </div>
        </body>
        </html>
        """
        
        # Send to customer
        customer_message = Mail(
            from_email=Email("hello@citydiscoverer.ai", "City Discoverer Team"),
            to_emails=To(email),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        customer_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send copy to admin
        admin_subject = f"[ADMIN] Welcome Email Sent - {email}"
        admin_message = Mail(
            from_email=Email("hello@citydiscoverer.ai", "City Discoverer Team"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=admin_subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send both emails
        customer_response = sg.send(customer_message)
        admin_response = sg.send(admin_message)
        
        print(f"Welcome email sent! Customer: {customer_response.status_code}, Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send welcome email: {str(e)}")
        return False

def send_daily_summary_email(date_str=""):
    """Send daily booking summary email to admin"""
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured, skipping daily summary email")
        return False
    
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        
        # Use provided date or today
        if date_str:
            target_date = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        else:
            target_date = dt.date.today()
        
        # Get bookings for the day
        start_of_day = dt.datetime.combine(target_date, dt.time.min)
        end_of_day = dt.datetime.combine(target_date, dt.time.max)
        
        bookings = Booking.query.filter(
            Booking.start_dt.between(start_of_day, end_of_day)
        ).all()
        
        # Get pass purchases for the day
        passes = Pass.query.filter(
            Pass.purchase_dt.between(start_of_day, end_of_day)
        ).all()
        
        # Calculate totals
        total_bookings = len(bookings)
        total_revenue_cents = sum(b.amount_cents for b in bookings)
        total_passes = len(passes)
        pass_revenue_cents = sum((p.pass_type == "day" and 2000) or (p.pass_type == "week" and 10000) or 15000 for p in passes)
        
        subject = f"Daily Summary - {target_date.strftime('%B %d, %Y')}"
        
        # Create booking rows HTML
        booking_rows = ""
        for booking in bookings:
            status_color = {"confirmed": "#28a745", "cancelled": "#dc3545", "completed": "#6c757d"}.get(booking.status, "#17a2b8")
            booking_rows += f"""
            <tr style="border-bottom: 1px solid #dee2e6;">
                <td style="padding: 8px; font-size: 14px;">{booking.start_dt.strftime('%I:%M %p')}</td>
                <td style="padding: 8px; font-size: 14px;">{booking.resource.name}</td>
                <td style="padding: 8px; font-size: 14px;">{booking.email}</td>
                <td style="padding: 8px; font-size: 14px;">{booking.duration_hours}h</td>
                <td style="padding: 8px; font-size: 14px; color: {status_color}; font-weight: bold;">{booking.status.upper()}</td>
                <td style="padding: 8px; font-size: 14px; font-weight: bold;">{as_money(booking.amount_cents)}</td>
            </tr>
            """
        
        if not booking_rows:
            booking_rows = '<tr><td colspan="6" style="padding: 15px; text-align: center; color: #6c757d; font-style: italic;">No bookings today</td></tr>'
        
        # Create pass rows HTML
        pass_rows = ""
        for pass_obj in passes:
            pass_rows += f"""
            <tr style="border-bottom: 1px solid #dee2e6;">
                <td style="padding: 8px; font-size: 14px;">{pass_obj.purchase_dt.strftime('%I:%M %p')}</td>
                <td style="padding: 8px; font-size: 14px;">{pass_obj.pass_type.title()} Pass</td>
                <td style="padding: 8px; font-size: 14px;">{pass_obj.email}</td>
                <td style="padding: 8px; font-size: 14px;">{pass_obj.valid_to.strftime('%m/%d')}</td>
                <td style="padding: 8px; font-size: 14px; color: #28a745; font-weight: bold;">{pass_obj.status.upper()}</td>
            </tr>
            """
        
        if not pass_rows:
            pass_rows = '<tr><td colspan="5" style="padding: 15px; text-align: center; color: #6c757d; font-style: italic;">No passes purchased today</td></tr>'
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #495057; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">📊 Daily Summary</h2>
                <p style="margin: 10px 0 0 0; font-size: 16px;">{target_date.strftime('%A, %B %d, %Y')}</p>
            </div>
            
            <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                <div style="flex: 1; background-color: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 8px; text-align: center;">
                    <h4 style="margin: 0 0 5px 0; color: #155724;">Total Bookings</h4>
                    <div style="font-size: 24px; font-weight: bold; color: #155724;">{total_bookings}</div>
                </div>
                <div style="flex: 1; background-color: #cff4fc; border: 1px solid #b6effb; padding: 15px; border-radius: 8px; text-align: center;">
                    <h4 style="margin: 0 0 5px 0; color: #055160;">Passes Sold</h4>
                    <div style="font-size: 24px; font-weight: bold; color: #055160;">{total_passes}</div>
                </div>
                <div style="flex: 1; background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 8px; text-align: center;">
                    <h4 style="margin: 0 0 5px 0; color: #856404;">Total Revenue</h4>
                    <div style="font-size: 20px; font-weight: bold; color: #856404;">{as_money(total_revenue_cents + pass_revenue_cents)}</div>
                </div>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">📅 Today's Bookings</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <thead style="background-color: #f8f9fa;">
                        <tr>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Time</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Workspace</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Customer</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Duration</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Status</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Revenue</th>
                        </tr>
                    </thead>
                    <tbody>
                        {booking_rows}
                    </tbody>
                </table>
            </div>
            
            <div style="background-color: #ffffff; border: 1px solid #dee2e6; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h3 style="color: #495057; margin-top: 0;">🎫 Today's Pass Sales</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <thead style="background-color: #f8f9fa;">
                        <tr>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Time</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Pass Type</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Customer</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Expires</th>
                            <th style="padding: 10px 8px; text-align: left; font-size: 14px; color: #495057;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {pass_rows}
                    </tbody>
                </table>
            </div>
            
            <div style="text-align: center; color: #6c757d; font-size: 12px; margin-top: 30px;">
                <p>Generated at {dt.datetime.utcnow().strftime('%B %d, %Y at %I:%M %p')} UTC<br>
                EasyDesk Booking System - City Discoverer</p>
            </div>
        </body>
        </html>
        """
        
        # Send to admin only
        admin_message = Mail(
            from_email=Email("billing@citydiscoverer.ai", "EasyDesk System"),
            to_emails=To("hello@citydiscoverer.ai"),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        admin_message.reply_to = Email("hello@citydiscoverer.ai")
        
        # Send admin email
        admin_response = sg.send(admin_message)
        
        print(f"Daily summary email sent! Admin: {admin_response.status_code}")
        return True
        
    except Exception as e:
        print(f"Failed to send daily summary email: {str(e)}")
        return False

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
    flow_type = request.args.get("flow", "workspace-first")  # Default to workspace-first
    grouped = day_bookings(date)
    
    # Determine flow type based on URL parameters
    if pre_selected_plan and not request.args.get("flow"):
        flow_type = "plan-first"
    
    # Calculate capacity for each resource for the selected date
    capacity_info = {}
    for r in resources:
        # For simplicity, show capacity for 9 AM - 5 PM window
        start_dt = parse_dt(date, "09:00")
        end_dt = parse_dt(date, "17:00")
        available = seats_left(r.id, start_dt, end_dt)
        capacity_info[r.id] = {"available": available, "total": r.capacity}
    
    # Plan type names for display
    plan_display_names = {
        "hour": "Hourly",
        "day": "Day Pass", 
        "week": "Week Pass",
        "month": "Month Pass"
    }
    
    return render_template("book.html", 
                         resources=resources, 
                         promo=PROMO_CODE, 
                         date=date, 
                         grouped=grouped,
                         capacity_info=capacity_info,
                         as_money=as_money,
                         use_mercury=USE_MERCURY,
                         allow_pos=ALLOW_POS_CHECKOUT,
                         pre_selected_plan=pre_selected_plan,
                         flow_type=flow_type,
                         plan_display_names=plan_display_names)

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
        # Stripe online payment
        if stripe.api_key and amount_cents > 0:
            try:
                # Create Stripe checkout session
                checkout_session = stripe.checkout.Session.create(
                    line_items=[
                        {
                            'price_data': {
                                'currency': 'usd',
                                'product_data': {
                                    'name': f"{resource.name} - {plan_type.title()} Pass",
                                    'description': f"{seats} seat(s) for {resource.name}",
                                },
                                'unit_amount': amount_cents,
                            },
                            'quantity': 1,
                        },
                    ],
                    mode='payment',
                    customer_email=email,
                    success_url=f'https://{YOUR_DOMAIN}/success-stripe?session_id={{CHECKOUT_SESSION_ID}}&bid={booking.id}',
                    cancel_url=f'https://{YOUR_DOMAIN}/book?cancelled=true',
                    metadata={
                        'booking_id': str(booking.id),
                        'email': email,
                        'resource_name': resource.name,
                    }
                )
                
                # Save payment record
                payment = Payment()
                payment.booking_id = booking.id
                payment.provider = "stripe"
                payment.intent_id = checkout_session.id
                payment.status = "created"
                payment.amount_cents = amount_cents
                db.session.add(payment)
                db.session.commit()
                
                return redirect(checkout_session.url)
                
            except Exception as e:
                flash("Payment system unavailable. Please try again.", "error")
                return redirect(url_for("book_page"))
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
    
    # Send confirmation email
    send_booking_confirmation_email(booking)
    
    return render_template("success.html", booking=booking, as_money=as_money)

@app.get("/success-pos")
def success_pos():
    bid_str = request.args.get("bid")
    if not bid_str:
        abort(400)
    bid = int(bid_str)
    booking = Booking.query.get_or_404(bid)
    
    # Send confirmation email
    send_booking_confirmation_email(booking)
    
    return render_template("success_pos.html", booking=booking, as_money=as_money)

@app.get("/success-pos-pass")
def success_pos_pass():
    pid_str = request.args.get("pid")
    if not pid_str:
        abort(400)
    pid = int(pid_str)
    pass_obj = Pass.query.get_or_404(pid)
    return render_template("success_pos_pass.html", pass_obj=pass_obj)

@app.get("/success-stripe")
def success_stripe():
    """Handle successful Stripe payment"""
    session_id = request.args.get("session_id")
    bid_str = request.args.get("bid")
    
    if not session_id or not bid_str:
        abort(400)
    
    bid = int(bid_str)
    booking = Booking.query.get_or_404(bid)
    
    # Verify the payment with Stripe
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status == 'paid':
            # Update booking status
            booking.status = "confirmed"
            
            # Update payment record
            payment = Payment.query.filter_by(booking_id=bid, provider="stripe").first()
            if payment:
                payment.status = "paid"
                payment.intent_id = session.payment_intent
            
            db.session.commit()
            
            # Send confirmation email
            send_booking_confirmation_email(booking)
            
            return render_template("success.html", booking=booking, as_money=as_money, payment_method="stripe")
        else:
            flash("Payment verification failed. Please contact support.", "error")
            return redirect(url_for("book_page"))
            
    except Exception as e:
        flash("Payment verification error. Please contact support.", "error")
        return redirect(url_for("book_page"))

@app.get("/cancel")
def cancel():
    bid = request.args.get("bid")
    return render_template("cancel.html", bid=bid)

@app.get("/test-emails")
def test_emails():
    """Send test emails to verify email templates"""
    test_recipients = ["felixabayomi@icloud.com", "felabayomi@gmail.com"]
    
    result = send_test_emails(test_recipients)
    
    if "error" in result:
        return f"<h2>❌ Test Email Failed</h2><p>Error: {result['error']}</p>", 500
    else:
        return f"""
        <h2>✅ Test Emails Sent Successfully!</h2>
        <p><strong>Recipients:</strong> {', '.join(result['recipients'])}</p>
        <p><strong>Status Code:</strong> {result['status_code']}</p>
        <p><strong>Subject:</strong> [TEST] Booking Confirmation - Hot Desk #5</p>
        <hr>
        <h3>Email Template Preview:</h3>
        <ul>
            <li>🎨 Professional HTML formatting</li>
            <li>📧 From: billing@citydiscoverer.ai</li>
            <li>↩️ Reply-to: hello@citydiscoverer.ai</li>
            <li>📋 Sample booking details (Hot Desk #5, 3 hours, $15.00)</li>
            <li>📍 Location information</li>
            <li>⚡ Clear TEST indicator at the top</li>
        </ul>
        <p>Check your inbox at both email addresses to see how the template looks!</p>
        <p><a href="/">← Back to EasyDesk</a></p>
        """

@app.get("/check-reminders")
def manual_check_reminders():
    """Manually trigger reminder checking (for testing and cron jobs)"""
    result = check_and_send_reminders()
    
    if "error" in result:
        return f"<h2>❌ Reminder Check Failed</h2><p>Error: {result['error']}</p>", 500
    else:
        return f"""
        <h2>✅ Reminder Check Complete</h2>
        <p><strong>24-hour reminders sent:</strong> {result['sent_24h']}</p>
        <p><strong>2-hour reminders sent:</strong> {result['sent_2h']}</p>
        <p><strong>Total reminders:</strong> {int(result['sent_24h']) + int(result['sent_2h'])}</p>
        <hr>
        <p>This endpoint should be called regularly (every hour) for automatic reminders.</p>
        <p><a href="/">← Back to EasyDesk</a></p>
        """

@app.get("/check-pass-notifications")
def manual_check_pass_notifications():
    """Manually trigger pass notification checking"""
    result = check_and_send_pass_notifications()
    
    if "error" in result:
        return f"<h2>❌ Pass Notification Check Failed</h2><p>Error: {result['error']}</p>", 500
    else:
        return f"""
        <h2>✅ Pass Notification Check Complete</h2>
        <p><strong>Purchase confirmations sent:</strong> {result['sent_confirmations']}</p>
        <p><strong>Expiration warnings sent:</strong> {result['sent_warnings']}</p>
        <p><strong>Total notifications:</strong> {int(result['sent_confirmations']) + int(result['sent_warnings'])}</p>
        <hr>
        <p>This endpoint should be called regularly for automatic pass notifications.</p>
        <p><a href="/">← Back to EasyDesk</a></p>
        """

@app.get("/admin/email-management")
def admin_email_management():
    """Admin interface for email management and manual triggers"""
    return f"""
    <html>
    <head>
        <title>Admin Email Management - EasyDesk</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            .email-card {{ margin-bottom: 20px; }}
            .trigger-section {{ background-color: #f8f9fa; padding: 20px; margin-bottom: 20px; border-radius: 8px; }}
        </style>
    </head>
    <body>
        <div class="container mt-4">
            <h2>📧 Email Management Center</h2>
            <p class="text-muted">Manage and trigger email notifications for EasyDesk booking system</p>
            
            <div class="row">
                <div class="col-md-6">
                    <div class="trigger-section">
                        <h4>🤖 Automatic Email Checks</h4>
                        <p class="text-muted">Check and send automated emails that are due</p>
                        <a href="/check-reminders" class="btn btn-primary me-2">Check Booking Reminders</a>
                        <a href="/check-pass-notifications" class="btn btn-info">Check Pass Notifications</a>
                    </div>
                </div>
                
                <div class="col-md-6">
                    <div class="trigger-section">
                        <h4>📊 Admin Reports</h4>
                        <p class="text-muted">Generate daily summaries and reports</p>
                        <a href="/send-daily-summary" class="btn btn-secondary me-2">Send Today's Summary</a>
                        <a href="/send-daily-summary/2025-09-24" class="btn btn-outline-secondary">Yesterday's Summary</a>
                    </div>
                </div>
            </div>
            
            <div class="row">
                <div class="col-12">
                    <div class="trigger-section">
                        <h4>📬 Manual Email Triggers</h4>
                        <p class="text-muted">Send specific emails manually (requires booking/pass IDs)</p>
                        
                        <div class="row">
                            <div class="col-md-4">
                                <h6>Payment & Refund Emails</h6>
                                <div class="mb-3">
                                    <button class="btn btn-outline-danger btn-sm w-100" onclick="sendPaymentFailure()">Send Payment Failure</button>
                                </div>
                                <div class="mb-3">
                                    <button class="btn btn-outline-success btn-sm w-100" onclick="sendRefundConfirmation()">Send Refund Confirmation</button>
                                </div>
                            </div>
                            
                            <div class="col-md-4">
                                <h6>Booking Change Emails</h6>
                                <div class="mb-3">
                                    <button class="btn btn-outline-warning btn-sm w-100" onclick="sendCancellation()">Send Booking Cancellation</button>
                                </div>
                                <div class="mb-3">
                                    <button class="btn btn-outline-info btn-sm w-100" onclick="sendModification()">Send Booking Modification</button>
                                </div>
                            </div>
                            
                            <div class="col-md-4">
                                <h6>Customer Emails</h6>
                                <div class="mb-3">
                                    <button class="btn btn-outline-primary btn-sm w-100" onclick="sendWelcome()">Send Welcome Email</button>
                                </div>
                            </div>
                        </div>
                        
                        <div class="alert alert-warning mt-3">
                            <strong>Note:</strong> Manual trigger buttons require JavaScript prompts to gather booking/pass IDs and other required parameters. 
                            For production use, these would be integrated into the admin booking management interface.
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="row">
                <div class="col-12">
                    <div class="trigger-section">
                        <h4>📈 Email System Status</h4>
                        <div class="row">
                            <div class="col-md-3 text-center">
                                <div class="card">
                                    <div class="card-body">
                                        <h5 class="card-title text-success">✅ Automatic</h5>
                                        <p class="card-text">
                                            • Booking Confirmations<br>
                                            • 24h/2h Reminders<br>
                                            • Pass Purchase/Expiry
                                        </p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-3 text-center">
                                <div class="card">
                                    <div class="card-body">
                                        <h5 class="card-title text-warning">🔧 Manual</h5>
                                        <p class="card-text">
                                            • Payment Failures<br>
                                            • Refund Confirmations<br>
                                            • Booking Changes
                                        </p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-3 text-center">
                                <div class="card">
                                    <div class="card-body">
                                        <h5 class="card-title text-info">🎯 Admin</h5>
                                        <p class="card-text">
                                            • Welcome Emails<br>
                                            • Daily Summaries<br>
                                            • Customer Onboarding
                                        </p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-3 text-center">
                                <div class="card">
                                    <div class="card-body">
                                        <h5 class="card-title text-primary">📧 SendGrid</h5>
                                        <p class="card-text">
                                            • Dual Recipients<br>
                                            • Professional Templates<br>
                                            • Error Tracking
                                        </p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <p class="text-center mt-4">
                <a href="/" class="btn btn-outline-primary">← Back to EasyDesk</a>
            </p>
        </div>
        
        <script>
            function sendPaymentFailure() {{
                const bookingId = prompt("Enter Booking or Pass ID for payment failure email:");
                if (bookingId) {{
                    alert("Payment failure email functionality would trigger here with ID: " + bookingId);
                    // In production: fetch(`/admin/send-payment-failure/${{bookingId}}`)
                }}
            }}
            
            function sendRefundConfirmation() {{
                const bookingId = prompt("Enter Booking or Pass ID for refund confirmation:");
                const amount = prompt("Enter refund amount in cents (e.g., 2500 for $25.00):");
                if (bookingId && amount) {{
                    alert(`Refund confirmation would be sent for ID: ${{bookingId}}, Amount: ${{amount}} cents`);
                    // In production: fetch(`/admin/send-refund/${{bookingId}}/${{amount}}`)
                }}
            }}
            
            function sendCancellation() {{
                const bookingId = prompt("Enter Booking ID for cancellation email:");
                if (bookingId) {{
                    alert("Cancellation email functionality would trigger here with Booking ID: " + bookingId);
                    // In production: fetch(`/admin/send-cancellation/${{bookingId}}`)
                }}
            }}
            
            function sendModification() {{
                const bookingId = prompt("Enter Booking ID for modification email:");
                if (bookingId) {{
                    alert("Modification email functionality would trigger here with Booking ID: " + bookingId);
                    // In production: fetch(`/admin/send-modification/${{bookingId}}`)
                }}
            }}
            
            function sendWelcome() {{
                const email = prompt("Enter customer email for welcome email:");
                const name = prompt("Enter customer name (optional):");
                if (email) {{
                    alert(`Welcome email would be sent to: ${{email}} ${{name ? 'Name: ' + name : ''}}`);
                    // In production: fetch(`/admin/send-welcome/${{email}}/${{name}}`)
                }}
            }}
        </script>
    </body>
    </html>
    """

@app.get("/send-daily-summary")
@app.get("/send-daily-summary/<date>")
def manual_send_daily_summary(date=None):
    """Manually send daily summary email"""
    result = send_daily_summary_email(date if date else "")
    
    if result:
        return f"""
        <h2>✅ Daily Summary Sent Successfully</h2>
        <p>Daily summary email for {date if date else 'today'} has been sent to admin.</p>
        <hr>
        <p><a href="/admin/email-management">← Back to Email Management</a></p>
        <p><a href="/">← Back to EasyDesk</a></p>
        """
    else:
        return f"""
        <h2>❌ Daily Summary Failed</h2>
        <p>Failed to send daily summary email. Check server logs for details.</p>
        <hr>
        <p><a href="/admin/email-management">← Back to Email Management</a></p>
        <p><a href="/">← Back to EasyDesk</a></p>
        """, 500

@app.get("/run-all-email-checks")
def run_all_automatic_email_checks():
    """Run all automatic email checks - ideal for cron job/scheduler"""
    try:
        # Check and send booking reminders
        reminder_result = check_and_send_reminders()
        
        # Check and send pass notifications  
        pass_result = check_and_send_pass_notifications()
        
        # Calculate totals
        total_emails_sent = 0
        if "sent_24h" in reminder_result and "sent_2h" in reminder_result:
            total_emails_sent += int(reminder_result["sent_24h"]) + int(reminder_result["sent_2h"])
        
        if "sent_confirmations" in pass_result and "sent_warnings" in pass_result:
            total_emails_sent += int(pass_result["sent_confirmations"]) + int(pass_result["sent_warnings"])
        
        # Check for errors
        errors = []
        if "error" in reminder_result:
            errors.append(f"Reminders: {reminder_result['error']}")
        if "error" in pass_result:
            errors.append(f"Passes: {pass_result['error']}")
        
        if errors:
            return f"""
            <h2>⚠️ Email Check Completed with Errors</h2>
            <p><strong>Total emails sent:</strong> {total_emails_sent}</p>
            <div class="alert alert-warning">
                <strong>Errors encountered:</strong><br>
                {'<br>'.join(errors)}
            </div>
            <hr>
            <p><strong>Reminder Results:</strong> {reminder_result}</p>
            <p><strong>Pass Results:</strong> {pass_result}</p>
            <hr>
            <p><a href="/admin/email-management">← Back to Email Management</a></p>
            <p><a href="/">← Back to EasyDesk</a></p>
            """, 500
        else:
            return f"""
            <h2>✅ All Email Checks Complete</h2>
            <p><strong>Total emails sent:</strong> {total_emails_sent}</p>
            
            <div class="row mt-3">
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-header bg-primary text-white">📅 Booking Reminders</div>
                        <div class="card-body">
                            <p><strong>24-hour reminders:</strong> {reminder_result.get('sent_24h', 0)}</p>
                            <p><strong>2-hour reminders:</strong> {reminder_result.get('sent_2h', 0)}</p>
                        </div>
                    </div>
                </div>
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-header bg-info text-white">🎫 Pass Notifications</div>
                        <div class="card-body">
                            <p><strong>Purchase confirmations:</strong> {pass_result.get('sent_confirmations', 0)}</p>
                            <p><strong>Expiration warnings:</strong> {pass_result.get('sent_warnings', 0)}</p>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="alert alert-info mt-3">
                <h5>🤖 Automation Setup</h5>
                <p><strong>For Production:</strong> Schedule this endpoint to run every hour with a cron job or monitoring service:</p>
                <code>curl -X GET https://your-domain.com/run-all-email-checks</code>
                
                <p class="mt-2"><strong>Example Cron Job (every hour):</strong></p>
                <code>0 * * * * curl -s https://your-domain.com/run-all-email-checks > /dev/null</code>
            </div>
            
            <hr>
            <p>Last run: {dt.datetime.utcnow().strftime('%B %d, %Y at %I:%M:%S %p')} UTC</p>
            <p><a href="/admin/email-management">← Back to Email Management</a></p>
            <p><a href="/">← Back to EasyDesk</a></p>
            """
            
    except Exception as e:
        return f"""
        <h2>❌ Email Check System Failed</h2>
        <p>Critical error in email check system: {str(e)}</p>
        <hr>
        <p><a href="/admin/email-management">← Back to Email Management</a></p>
        <p><a href="/">← Back to EasyDesk</a></p>
        """, 500

@app.get("/email-system-status")
def email_system_status():
    """Get comprehensive email system status and statistics"""
    try:
        # Get recent bookings that should have confirmations
        recent_bookings = Booking.query.filter(
            Booking.created_at >= dt.datetime.utcnow() - dt.timedelta(days=7)
        ).count()
        
        # Get recent passes
        recent_passes = Pass.query.filter(
            Pass.purchase_dt >= dt.datetime.utcnow() - dt.timedelta(days=7)  
        ).count()
        
        # Get upcoming bookings for reminders
        tomorrow = dt.datetime.utcnow() + dt.timedelta(hours=24)
        upcoming_bookings_24h = Booking.query.filter(
            Booking.start_dt.between(
                dt.datetime.utcnow() + dt.timedelta(hours=23),
                dt.datetime.utcnow() + dt.timedelta(hours=25)
            ),
            Booking.reminder_24h_sent == False
        ).count()
        
        upcoming_bookings_2h = Booking.query.filter(
            Booking.start_dt.between(
                dt.datetime.utcnow() + dt.timedelta(hours=1, minutes=30),
                dt.datetime.utcnow() + dt.timedelta(hours=2, minutes=30)
            ),
            Booking.reminder_2h_sent == False
        ).count()
        
        # Get expiring passes (next 2 days)
        two_days_from_now = dt.datetime.utcnow() + dt.timedelta(days=2)
        one_day_from_now = dt.datetime.utcnow() + dt.timedelta(days=1)
        expiring_passes = Pass.query.filter(
            Pass.status == "active",
            Pass.valid_to.between(one_day_from_now, two_days_from_now),
            Pass.expiration_warning_sent == False
        ).count()
        
        # Get unconfirmed pass purchases
        unconfirmed_passes = Pass.query.filter(
            Pass.purchase_email_sent == False,
            Pass.status == "active"
        ).count()
        
        return f"""
        <html>
        <head>
            <title>Email System Status - EasyDesk</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        </head>
        <body>
            <div class="container mt-4">
                <h2>📊 Email System Status</h2>
                <p class="text-muted">Current status of automatic email triggers</p>
                
                <div class="row">
                    <div class="col-md-6">
                        <div class="card border-primary">
                            <div class="card-header bg-primary text-white">
                                <h5>📅 Pending Booking Reminders</h5>
                            </div>
                            <div class="card-body">
                                <div class="row text-center">
                                    <div class="col-6">
                                        <h3 class="text-primary">{upcoming_bookings_24h}</h3>
                                        <small>24-hour reminders due</small>
                                    </div>
                                    <div class="col-6">
                                        <h3 class="text-warning">{upcoming_bookings_2h}</h3>
                                        <small>2-hour reminders due</small>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="col-md-6">
                        <div class="card border-info">
                            <div class="card-header bg-info text-white">
                                <h5>🎫 Pending Pass Notifications</h5>
                            </div>
                            <div class="card-body">
                                <div class="row text-center">
                                    <div class="col-6">
                                        <h3 class="text-success">{unconfirmed_passes}</h3>
                                        <small>Purchase confirmations due</small>
                                    </div>
                                    <div class="col-6">
                                        <h3 class="text-warning">{expiring_passes}</h3>
                                        <small>Expiration warnings due</small>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="row mt-4">
                    <div class="col-12">
                        <div class="card border-secondary">
                            <div class="card-header bg-secondary text-white">
                                <h5>📈 Recent Activity (Last 7 Days)</h5>
                            </div>
                            <div class="card-body">
                                <div class="row text-center">
                                    <div class="col-md-3">
                                        <h4 class="text-primary">{recent_bookings}</h4>
                                        <small>New Bookings</small>
                                    </div>
                                    <div class="col-md-3">
                                        <h4 class="text-info">{recent_passes}</h4>
                                        <small>Passes Sold</small>
                                    </div>
                                    <div class="col-md-3">
                                        <h4 class="text-success">{'✅' if SENDGRID_API_KEY else '❌'}</h4>
                                        <small>SendGrid Status</small>
                                    </div>
                                    <div class="col-md-3">
                                        <h4 class="text-warning">{upcoming_bookings_24h + upcoming_bookings_2h + expiring_passes + unconfirmed_passes}</h4>
                                        <small>Total Pending Emails</small>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="row mt-4">
                    <div class="col-12">
                        <div class="card">
                            <div class="card-header">
                                <h5>🔧 Actions</h5>
                            </div>
                            <div class="card-body">
                                <a href="/run-all-email-checks" class="btn btn-success me-2">🚀 Run All Email Checks Now</a>
                                <a href="/admin/email-management" class="btn btn-primary me-2">📧 Email Management</a>
                                <a href="/" class="btn btn-outline-secondary">← Back to EasyDesk</a>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="alert alert-info mt-4">
                    <h6>🤖 Automation Recommendations:</h6>
                    <ul>
                        <li><strong>High Priority:</strong> Run <code>/run-all-email-checks</code> every hour for timely notifications</li>
                        <li><strong>Daily Reports:</strong> Schedule <code>/send-daily-summary</code> at end of business day</li>
                        <li><strong>Monitoring:</strong> Check <code>/email-system-status</code> for pending notifications</li>
                    </ul>
                </div>
            </div>
        </body>
        </html>
        """
        
    except Exception as e:
        return f"<h2>❌ Status Check Failed</h2><p>Error: {str(e)}</p>", 500

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