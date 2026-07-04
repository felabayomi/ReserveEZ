import datetime as dt
import json
from flask import (Blueprint, render_template, request, redirect, url_for, abort,
                   jsonify, flash, make_response)
import stripe
from models import db, Restaurant, Table, Reservation, WaitlistEntry, NoShowRecord, PromoCode, PaymentTransaction, RestaurantNomination
from config import (
    STRIPE_PUBLIC_KEY,
    STRIPE_WEBHOOK_SECRET,
    CUISINE_TYPES,
    BASE_URL,
    SHOW_TEMPLATE_RESTAURANTS,
    TEMPLATE_RESTAURANT_SLUGS,
)
from helpers import (as_money, generate_time_slots, get_day_key, find_available_tables,
                     get_no_show_count, calculate_end_time, notify_waitlist_for_slot, make_slug)
from email_service import (send_confirmation_email, send_reminder_email,
                           send_cancellation_email, send_no_show_email,
                           send_restaurant_new_reservation, send_deposit_receipt,
                           send_restaurant_cancellation, send_deposit_refund_email,
                           send_restaurant_no_show, send_admin_deposit_failed,
                           send_admin_nomination,
                           send_restaurant_deposit_received)

public_bp = Blueprint("public", __name__)


def _is_hidden_template(restaurant):
    return (not SHOW_TEMPLATE_RESTAURANTS) and restaurant.slug in TEMPLATE_RESTAURANT_SLUGS


@public_bp.route("/")
def index():
    restaurants = Restaurant.query.filter_by(active=True).order_by(Restaurant.name).all()
    if not SHOW_TEMPLATE_RESTAURANTS:
        restaurants = [r for r in restaurants if r.slug not in TEMPLATE_RESTAURANT_SLUGS]
    cuisine_filter = request.args.get("cuisine", "")
    if cuisine_filter:
        restaurants = [r for r in restaurants if r.cuisine_type == cuisine_filter]
    nomination_count = RestaurantNomination.query.count()
    return render_template("index.html", restaurants=restaurants,
                           cuisine_types=CUISINE_TYPES, cuisine_filter=cuisine_filter,
                           as_money=as_money, nomination_count=nomination_count)


@public_bp.route("/restaurant/<slug>")
def restaurant_detail(slug):
    restaurant = Restaurant.query.filter_by(slug=slug, active=True).first_or_404()
    if _is_hidden_template(restaurant):
        abort(404)
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


@public_bp.route("/reserve/<slug>", methods=["GET", "POST"])
def reserve(slug):
    restaurant = Restaurant.query.filter_by(slug=slug, active=True).first_or_404()
    if _is_hidden_template(restaurant):
        abort(404)

    if request.method == "GET":
        date_str = request.args.get("date")
        time_str = request.args.get("time")
        party_size = request.args.get("party_size", 2, type=int)

        if not date_str or not time_str:
            return redirect(url_for("public.restaurant_detail", slug=slug))

        try:
            res_date = dt.date.fromisoformat(date_str)
            res_time = dt.datetime.strptime(time_str, "%H:%M").time()
        except:
            return redirect(url_for("public.restaurant_detail", slug=slug))

        end_time = calculate_end_time(res_time, restaurant.slot_duration_minutes)
        tables = find_available_tables(restaurant.id, res_date, res_time, end_time, party_size)

        if not tables:
            flash("Sorry, that time slot is no longer available.", "error")
            return redirect(url_for("public.restaurant_detail", slug=slug,
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
            return redirect(url_for("public.reserve", slug=slug, date=date_str,
                                    time=time_str, party_size=party_size))

        res_date = dt.date.fromisoformat(date_str)
        res_time = dt.datetime.strptime(time_str, "%H:%M").time()
        end_time = calculate_end_time(res_time, restaurant.slot_duration_minutes)

        tables = find_available_tables(restaurant.id, res_date, res_time, end_time, party_size)
        if not tables:
            flash("Sorry, that time slot is no longer available.", "error")
            return redirect(url_for("public.restaurant_detail", slug=slug))

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
                payment_kwargs = {
                    "amount": actual_deposit,
                    "currency": "usd",
                    "payment_method": payment_method_id,
                    "confirm": True,
                    "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                    "metadata": {"reservation_uuid": reservation.uuid,
                                 "restaurant": restaurant.name}
                }

                platform_fee = 0
                if restaurant.stripe_connect_id and restaurant.stripe_charges_enabled:
                    platform_fee = int(actual_deposit * restaurant.platform_fee_percent / 100)
                    payment_kwargs["application_fee_amount"] = platform_fee
                    payment_kwargs["transfer_data"] = {"destination": restaurant.stripe_connect_id}

                intent = stripe.PaymentIntent.create(**payment_kwargs)
                reservation.stripe_payment_intent_id = intent.id
                reservation.deposit_paid = True

                db.session.add(reservation)
                db.session.flush()

                txn = PaymentTransaction(
                    reservation_id=reservation.id,
                    restaurant_id=restaurant.id,
                    transaction_type="deposit",
                    amount_cents=actual_deposit,
                    platform_fee_cents=platform_fee if restaurant.stripe_connect_id and restaurant.stripe_charges_enabled else actual_deposit,
                    restaurant_amount_cents=actual_deposit - platform_fee if restaurant.stripe_connect_id and restaurant.stripe_charges_enabled else 0,
                    stripe_payment_intent_id=intent.id,
                    status="completed"
                )
                db.session.add(txn)
            except stripe.StripeError as e:
                try:
                    send_admin_deposit_failed(reservation, str(e))
                except Exception as notify_err:
                    print(f"Admin deposit failed notification error: {notify_err}")
                flash(f"Payment failed: {str(e)}", "error")
                return redirect(url_for("public.reserve", slug=slug, date=date_str,
                                        time=time_str, party_size=party_size))
        elif actual_deposit == 0 or waive_deposit:
            reservation.deposit_paid = True

        db.session.add(reservation)
        db.session.commit()

        try:
            send_confirmation_email(reservation)
        except Exception as e:
            print(f"Email error: {e}")

        try:
            send_restaurant_new_reservation(reservation)
        except Exception as e:
            print(f"Restaurant notification error: {e}")

        if reservation.deposit_paid and reservation.deposit_amount_cents > 0:
            try:
                send_deposit_receipt(reservation)
            except Exception as e:
                print(f"Deposit receipt error: {e}")
            try:
                send_restaurant_deposit_received(reservation, reservation.deposit_amount_cents)
            except Exception as e:
                print(f"Restaurant deposit notification error: {e}")

        return redirect(url_for("public.confirmation", res_uuid=reservation.uuid))

    except Exception as e:
        db.session.rollback()
        print(f"Reservation error: {e}")
        flash("An error occurred creating your reservation. Please try again.", "error")
        return redirect(url_for("public.restaurant_detail", slug=slug))


@public_bp.route("/confirmation/<res_uuid>")
def confirmation(res_uuid):
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()
    manage_url = f"{BASE_URL}/manage/{reservation.uuid}/{reservation.get_manage_token()}"
    calendar_url = f"{BASE_URL}/calendar/{reservation.uuid}.ics"
    return render_template("confirmation.html", r=reservation,
                           manage_url=manage_url, calendar_url=calendar_url,
                           as_money=as_money)


@public_bp.route("/manage/<res_uuid>/<token>")
def manage_reservation(res_uuid, token):
    verified_uuid = Reservation.verify_manage_token(token)
    if not verified_uuid or verified_uuid != res_uuid:
        abort(403)
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()
    return render_template("manage.html", r=reservation, token=token, as_money=as_money)


@public_bp.route("/cancel/<res_uuid>/<token>", methods=["POST"])
def cancel_reservation(res_uuid, token):
    verified_uuid = Reservation.verify_manage_token(token)
    if not verified_uuid or verified_uuid != res_uuid:
        abort(403)
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()

    if reservation.status in ["cancelled", "no_show", "completed"]:
        flash("This reservation cannot be cancelled.", "error")
        return redirect(url_for("public.manage_reservation", res_uuid=res_uuid, token=token))

    fee_charged = False
    fee_amount = 0

    if reservation.can_cancel_free:
        if reservation.deposit_paid and reservation.stripe_payment_intent_id and stripe.api_key:
            try:
                stripe.Refund.create(payment_intent=reservation.stripe_payment_intent_id)
                txn = PaymentTransaction(
                    reservation_id=reservation.id,
                    restaurant_id=reservation.restaurant_id,
                    transaction_type="refund",
                    amount_cents=reservation.deposit_amount_cents,
                    platform_fee_cents=0,
                    restaurant_amount_cents=0,
                    stripe_payment_intent_id=reservation.stripe_payment_intent_id,
                    status="completed"
                )
                db.session.add(txn)
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

    try:
        send_restaurant_cancellation(reservation)
    except Exception as e:
        print(f"Restaurant cancellation notification error: {e}")

    if reservation.can_cancel_free and reservation.deposit_paid and reservation.deposit_amount_cents > 0:
        try:
            send_deposit_refund_email(reservation, reservation.deposit_amount_cents)
        except Exception as e:
            print(f"Deposit refund email error: {e}")

    flash("Your reservation has been cancelled.", "success")
    return redirect(url_for("public.manage_reservation", res_uuid=res_uuid, token=token))


@public_bp.route("/confirm-attendance/<res_uuid>/<token>")
def confirm_attendance(res_uuid, token):
    verified_uuid = Reservation.verify_manage_token(token)
    if not verified_uuid or verified_uuid != res_uuid:
        abort(403)
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()
    reservation.guest_confirmed = True
    db.session.commit()
    flash("Thank you for confirming! We look forward to seeing you.", "success")
    return redirect(url_for("public.manage_reservation", res_uuid=res_uuid, token=token))


@public_bp.route("/calendar/<res_uuid>.ics")
def calendar_export(res_uuid):
    reservation = Reservation.query.filter_by(uuid=res_uuid).first_or_404()
    r = reservation.restaurant

    start_dt = dt.datetime.combine(reservation.reservation_date, reservation.reservation_time)
    end_dt = dt.datetime.combine(reservation.reservation_date, reservation.end_time)

    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//ReserveEZ//Reservation//EN
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


@public_bp.route("/waitlist/<slug>", methods=["POST"])
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
    return redirect(url_for("public.restaurant_detail", slug=slug,
                            date=entry.desired_date.isoformat(),
                            party_size=entry.party_size))


@public_bp.route("/create-setup-intent", methods=["POST"])
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


@public_bp.route("/api/validate-promo", methods=["POST"])
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


@public_bp.route("/stripe-webhook", methods=["POST"])
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

    event_type = event.get("type")

    if event_type == "payment_intent.succeeded":
        pi = event["data"]["object"]
        res_uuid = pi.get("metadata", {}).get("reservation_uuid")
        if res_uuid:
            reservation = Reservation.query.filter_by(uuid=res_uuid).first()
            if reservation:
                reservation.deposit_paid = True
                db.session.commit()

    elif event_type == "account.updated":
        account_data = event["data"]["object"]
        account_id = account_data.get("id")
        if account_id:
            restaurant = Restaurant.query.filter_by(stripe_connect_id=account_id).first()
            if restaurant:
                restaurant.stripe_charges_enabled = account_data.get("charges_enabled", False)
                restaurant.stripe_payouts_enabled = account_data.get("payouts_enabled", False)
                if account_data.get("charges_enabled"):
                    restaurant.stripe_connect_status = "active"
                db.session.commit()

    return "", 200


@public_bp.route("/cron/send-reminders")
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


@public_bp.route("/cron/process-no-shows")
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
                    ns_kwargs = {
                        "amount": fee_amount,
                        "currency": "usd",
                        "payment_method": r.stripe_payment_method_id,
                        "confirm": True,
                        "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                        "metadata": {"type": "no_show_fee",
                                     "reservation_uuid": r.uuid,
                                     "restaurant": r.restaurant.name}
                    }

                    ns_platform_fee = 0
                    if r.restaurant.stripe_connect_id and r.restaurant.stripe_charges_enabled:
                        ns_platform_fee = int(fee_amount * r.restaurant.platform_fee_percent / 100)
                        ns_kwargs["application_fee_amount"] = ns_platform_fee
                        ns_kwargs["transfer_data"] = {"destination": r.restaurant.stripe_connect_id}

                    charge = stripe.PaymentIntent.create(**ns_kwargs)
                    r.no_show_fee_charged = True
                    r.no_show_fee_amount_cents = fee_amount
                    record.fee_charged_cents = fee_amount

                    ns_txn = PaymentTransaction(
                        reservation_id=r.id,
                        restaurant_id=r.restaurant_id,
                        transaction_type="no_show_fee",
                        amount_cents=fee_amount,
                        platform_fee_cents=ns_platform_fee if r.restaurant.stripe_connect_id and r.restaurant.stripe_charges_enabled else fee_amount,
                        restaurant_amount_cents=fee_amount - ns_platform_fee if r.restaurant.stripe_connect_id and r.restaurant.stripe_charges_enabled else 0,
                        stripe_payment_intent_id=charge.id,
                        status="completed"
                    )
                    db.session.add(ns_txn)
                except Exception as e:
                    print(f"No-show charge error: {e}")

            db.session.add(record)
            processed += 1

            try:
                send_no_show_email(r, fee_amount)
            except:
                pass

            try:
                send_restaurant_no_show(r, fee_amount)
            except:
                pass

    db.session.commit()
    return jsonify({"no_shows_processed": processed})


@public_bp.route("/cron/expire-waitlist")
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


@public_bp.route("/about")
def about():
    return render_template("about.html")


@public_bp.route("/how-it-works/guests")
def how_it_works_guests():
    return render_template("how_it_works_guests.html")


@public_bp.route("/how-it-works/restaurants")
def how_it_works_restaurants():
    return render_template("how_it_works_restaurants.html")


@public_bp.route("/privacy")
def privacy():
    return render_template("privacy.html")


@public_bp.route("/terms")
def terms():
    return render_template("terms.html")


@public_bp.route("/data-policy")
def data_policy():
    return render_template("data_policy.html")


@public_bp.route("/nominate", methods=["GET", "POST"])
def nominate():
    if request.method == "POST":
        restaurant_name = request.form.get("restaurant_name", "").strip()
        city = request.form.get("city", "").strip()
        if not restaurant_name or not city:
            flash("Restaurant name and city are required.", "error")
            return redirect(url_for("public.nominate"))
        nom = RestaurantNomination(
            restaurant_name=restaurant_name,
            city=city,
            restaurant_email=request.form.get("restaurant_email", "").strip() or None,
            nominator_name=request.form.get("nominator_name", "").strip() or None,
            nominator_email=request.form.get("nominator_email", "").strip() or None,
        )
        db.session.add(nom)
        db.session.commit()
        try:
            send_admin_nomination(nom)
        except Exception as e:
            print(f"Nomination email error: {e}")
        flash("Thank you! Your nomination has been submitted. We'll reach out to them.", "success")
        return redirect(url_for("public.nominate"))
    nomination_count = RestaurantNomination.query.count()
    return render_template("nominate.html", nomination_count=nomination_count)


@public_bp.route("/api/availability", methods=["POST"])
def api_availability():
    data = request.get_json()
    restaurant_id = data.get("restaurant_id")
    date_str = data.get("date")
    party_size = data.get("party_size", 2)

    restaurant = Restaurant.query.get(restaurant_id)
    if not restaurant:
        return jsonify({"error": "Restaurant not found"}), 404
    if _is_hidden_template(restaurant):
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
