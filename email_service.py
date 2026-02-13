import datetime as dt
import resend
from flask import render_template
from models import db, NotificationLog
from config import BASE_URL, SENDER_NAME, SENDER_EMAIL
from helpers import as_money


def send_email(to_email, subject, html_content):
    if not resend.api_key:
        print(f"[EMAIL SKIP] No Resend key. Would send to {to_email}: {subject}")
        return False
    try:
        params = {
            "from": f"{SENDER_NAME} <{SENDER_EMAIL}>",
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        }
        resend.Emails.send(params)
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
