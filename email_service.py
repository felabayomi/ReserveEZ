import datetime as dt
import resend
from flask import render_template
from models import db, NotificationLog
from config import BASE_URL, SENDER_NAME, SENDER_EMAIL, ADMIN_EMAIL
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


def send_restaurant_new_reservation(reservation):
    restaurant = reservation.restaurant
    if not restaurant.email:
        return
    portal_url = f"{BASE_URL}/portal/reservations"
    html = render_template("emails/restaurant_new_reservation.html",
                           r=reservation, portal_url=portal_url, as_money=as_money)
    success = send_email(restaurant.email,
                         f"New Reservation - {reservation.guest_name} ({reservation.party_size} guests)",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="restaurant_new_reservation",
                              recipient_email=restaurant.email)
        db.session.add(log)
        db.session.commit()


def send_restaurant_cancellation(reservation):
    restaurant = reservation.restaurant
    if not restaurant.email:
        return
    portal_url = f"{BASE_URL}/portal/reservations"
    html = render_template("emails/restaurant_cancellation.html",
                           r=reservation, portal_url=portal_url, as_money=as_money)
    success = send_email(restaurant.email,
                         f"Reservation Cancelled - {reservation.guest_name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="restaurant_cancellation",
                              recipient_email=restaurant.email)
        db.session.add(log)
        db.session.commit()


def send_restaurant_deposit_received(reservation, amount_cents):
    restaurant = reservation.restaurant
    if not restaurant.email:
        return
    portal_url = f"{BASE_URL}/portal/reservations"
    html = render_template("emails/restaurant_deposit_received.html",
                           r=reservation, amount_cents=amount_cents,
                           portal_url=portal_url, as_money=as_money)
    success = send_email(restaurant.email,
                         f"Deposit Received - {as_money(amount_cents)} from {reservation.guest_name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="restaurant_deposit_received",
                              recipient_email=restaurant.email)
        db.session.add(log)
        db.session.commit()


def send_restaurant_no_show(reservation, fee_amount):
    restaurant = reservation.restaurant
    if not restaurant.email:
        return
    portal_url = f"{BASE_URL}/portal/no-show-stats"
    html = render_template("emails/restaurant_no_show.html",
                           r=reservation, fee_amount=fee_amount,
                           portal_url=portal_url, as_money=as_money)
    success = send_email(restaurant.email,
                         f"No-Show Recorded - {reservation.guest_name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="restaurant_no_show",
                              recipient_email=restaurant.email)
        db.session.add(log)
        db.session.commit()


def send_restaurant_stripe_connected(restaurant):
    if not restaurant.email:
        return
    earnings_url = f"{BASE_URL}/portal/earnings"
    html = render_template("emails/restaurant_stripe_connected.html",
                           restaurant=restaurant, earnings_url=earnings_url)
    success = send_email(restaurant.email,
                         f"Stripe Connected Successfully - {restaurant.name}",
                         html)
    if success:
        log = NotificationLog(notification_type="restaurant_stripe_connected",
                              recipient_email=restaurant.email)
        db.session.add(log)
        db.session.commit()


def send_admin_new_registration(restaurant_user):
    if not ADMIN_EMAIL:
        return
    admin_url = f"{BASE_URL}/admin/restaurants"
    html = render_template("emails/admin_new_registration.html",
                           user=restaurant_user, admin_url=admin_url)
    success = send_email(ADMIN_EMAIL,
                         f"New Restaurant Registration - {restaurant_user.restaurant.name}",
                         html)
    if success:
        log = NotificationLog(notification_type="admin_new_registration",
                              recipient_email=ADMIN_EMAIL)
        db.session.add(log)
        db.session.commit()


def send_admin_deposit_failed(reservation, error_message):
    if not ADMIN_EMAIL:
        return
    html = render_template("emails/admin_deposit_failed.html",
                           r=reservation, error_message=error_message, as_money=as_money)
    success = send_email(ADMIN_EMAIL,
                         f"Deposit Payment Failed - {reservation.guest_name} at {reservation.restaurant.name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="admin_deposit_failed",
                              recipient_email=ADMIN_EMAIL)
        db.session.add(log)
        db.session.commit()


def send_deposit_receipt(reservation):
    if not reservation.deposit_paid or reservation.deposit_amount_cents <= 0:
        return
    manage_url = f"{BASE_URL}/manage/{reservation.uuid}/{reservation.get_manage_token()}"
    html = render_template("emails/guest_deposit_receipt.html",
                           r=reservation, manage_url=manage_url, as_money=as_money)
    success = send_email(reservation.guest_email,
                         f"Deposit Receipt - {reservation.restaurant.name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="deposit_receipt",
                              recipient_email=reservation.guest_email)
        db.session.add(log)
        db.session.commit()


def send_deposit_refund_email(reservation, refund_amount):
    html = render_template("emails/guest_deposit_refund.html",
                           r=reservation, refund_amount=refund_amount, as_money=as_money)
    success = send_email(reservation.guest_email,
                         f"Deposit Refunded - {reservation.restaurant.name}",
                         html)
    if success:
        log = NotificationLog(reservation_id=reservation.id,
                              notification_type="deposit_refund",
                              recipient_email=reservation.guest_email)
        db.session.add(log)
        db.session.commit()
