import datetime as dt
import json
from flask import (Blueprint, render_template, request, redirect, url_for, abort,
                   flash, session)
import stripe
from models import db, Restaurant, Table, Reservation, NoShowRecord, WaitlistEntry, RestaurantUser
from config import CUISINE_TYPES, BASE_URL
from helpers import as_money, make_slug
from email_service import send_no_show_email, send_admin_new_registration, send_restaurant_stripe_connected, send_restaurant_no_show
from models import PaymentTransaction

portal_bp = Blueprint("portal", __name__)


def require_portal_login():
    if not session.get("portal_user_id"):
        from werkzeug.exceptions import HTTPException
        raise HTTPException(response=redirect(url_for("portal.login")))


def get_portal_user():
    uid = session.get("portal_user_id")
    if uid:
        return RestaurantUser.query.get(uid)
    return None


@portal_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = RestaurantUser.query.filter_by(email=email).first()
        if user and user.check_password(password):
            session["portal_user_id"] = user.id
            return redirect(url_for("portal.dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("portal/login.html")


@portal_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        restaurant_name = request.form.get("restaurant_name", "").strip()
        cuisine_type = request.form.get("cuisine_type", "")
        address = request.form.get("address", "").strip()
        restaurant_phone = request.form.get("restaurant_phone", "").strip()
        restaurant_email = request.form.get("restaurant_email", "").strip()
        description = request.form.get("description", "").strip()

        if RestaurantUser.query.filter_by(email=email).first():
            flash("An account with this email already exists.", "error")
            return render_template("portal/register.html", cuisine_types=CUISINE_TYPES)

        if not name or not email or not password or not restaurant_name:
            flash("Please fill in all required fields.", "error")
            return render_template("portal/register.html", cuisine_types=CUISINE_TYPES)

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("portal/register.html", cuisine_types=CUISINE_TYPES)

        restaurant = Restaurant(
            name=restaurant_name,
            slug=make_slug(restaurant_name),
            description=description,
            cuisine_type=cuisine_type,
            address=address,
            phone=restaurant_phone,
            email=restaurant_email,
            active=False,
        )
        db.session.add(restaurant)
        db.session.flush()

        user = RestaurantUser(
            email=email,
            name=name,
            phone=phone,
            restaurant_id=restaurant.id,
            approved=False,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        session["portal_user_id"] = user.id

        try:
            send_admin_new_registration(user)
        except Exception as e:
            print(f"Admin registration notification error: {e}")

        flash("Your restaurant has been submitted for review. You can start setting up your details while you wait for approval.", "success")
        return redirect(url_for("portal.dashboard"))

    return render_template("portal/register.html", cuisine_types=CUISINE_TYPES)


@portal_bp.route("/logout")
def logout():
    session.pop("portal_user_id", None)
    return redirect(url_for("portal.login"))


@portal_bp.route("", strict_slashes=False)
def dashboard():
    require_portal_login()
    user = get_portal_user()
    if not user:
        session.pop("portal_user_id", None)
        return redirect(url_for("portal.login"))

    restaurant = user.restaurant
    if not restaurant:
        flash("No restaurant linked to your account.", "error")
        return redirect(url_for("portal.login"))

    today = dt.date.today()
    today_reservations = Reservation.query.filter(
        Reservation.restaurant_id == restaurant.id,
        Reservation.reservation_date == today,
        Reservation.status.in_(["confirmed", "seated"]),
    ).order_by(Reservation.reservation_time).all()

    upcoming_count = Reservation.query.filter(
        Reservation.restaurant_id == restaurant.id,
        Reservation.reservation_date >= today,
        Reservation.status == "confirmed",
    ).count()

    no_show_count = NoShowRecord.query.filter(
        NoShowRecord.restaurant_id == restaurant.id,
        NoShowRecord.occurred_at >= dt.datetime.utcnow() - dt.timedelta(days=30),
    ).count()

    waitlist_count = WaitlistEntry.query.filter_by(
        restaurant_id=restaurant.id, status="waiting"
    ).count()

    return render_template("portal/dashboard.html",
                           user=user, restaurant=restaurant,
                           today_reservations=today_reservations,
                           upcoming_count=upcoming_count,
                           no_show_count=no_show_count,
                           waitlist_count=waitlist_count,
                           today=today, as_money=as_money)


@portal_bp.route("/restaurant/edit", methods=["GET", "POST"])
def restaurant_edit():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant
    if not restaurant:
        abort(404)

    if request.method == "POST":
        restaurant.name = request.form["name"]
        restaurant.slug = make_slug(request.form["name"])
        restaurant.description = request.form.get("description", "")
        restaurant.cuisine_type = request.form.get("cuisine_type", "")
        restaurant.address = request.form.get("address", "")
        restaurant.phone = request.form.get("restaurant_phone", request.form.get("phone", ""))
        restaurant.email = request.form.get("restaurant_email", request.form.get("email", ""))
        restaurant.image_url = request.form.get("image_url", "")
        restaurant.slot_duration_minutes = int(request.form.get("slot_duration_minutes", 90))
        restaurant.max_party_size = int(request.form.get("max_party_size", 12))
        restaurant.deposit_type = request.form.get("deposit_type", "per_person")
        restaurant.deposit_amount_cents = int(float(request.form.get("deposit_amount", 10)) * 100)
        restaurant.require_deposit = request.form.get("require_deposit") == "on"
        restaurant.require_card_hold = request.form.get("require_card_hold") == "on"
        restaurant.cancellation_cutoff_hours = int(request.form.get("cancellation_cutoff_hours", 24))
        restaurant.no_show_fee_cents = int(float(request.form.get("no_show_fee", 25)) * 100)
        restaurant.late_cancel_fee_cents = int(float(request.form.get("late_cancel_fee", 15)) * 100)

        hours_data = {}
        for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            periods = []
            opens = request.form.getlist(f"{day}_open")
            closes = request.form.getlist(f"{day}_close")
            for o, c in zip(opens, closes):
                if o and c:
                    periods.append([o, c])
            hours_data[day] = periods
        restaurant.opening_hours = json.dumps(hours_data)

        db.session.commit()
        flash("Restaurant details updated!", "success")
        return redirect(url_for("portal.dashboard"))

    return render_template("portal/restaurant_edit.html",
                           restaurant=restaurant, cuisine_types=CUISINE_TYPES)


@portal_bp.route("/tables", methods=["GET"])
def tables():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant
    if not restaurant:
        abort(404)
    return render_template("portal/tables.html", restaurant=restaurant)


@portal_bp.route("/table/add", methods=["POST"])
def table_add():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant
    t = Table(
        restaurant_id=restaurant.id,
        name=request.form["name"],
        capacity=int(request.form.get("capacity", 4)),
        table_type=request.form.get("table_type", "standard"),
    )
    db.session.add(t)
    db.session.commit()
    flash("Table added!", "success")
    return redirect(url_for("portal.tables"))


@portal_bp.route("/table/<int:tid>/edit", methods=["POST"])
def table_edit(tid):
    require_portal_login()
    user = get_portal_user()
    t = Table.query.get_or_404(tid)
    if t.restaurant_id != user.restaurant_id:
        abort(403)
    t.name = request.form["name"]
    t.capacity = int(request.form.get("capacity", 4))
    t.table_type = request.form.get("table_type", "standard")
    t.active = request.form.get("active") == "on"
    db.session.commit()
    flash("Table updated!", "success")
    return redirect(url_for("portal.tables"))


@portal_bp.route("/table/<int:tid>/delete", methods=["POST"])
def table_delete(tid):
    require_portal_login()
    user = get_portal_user()
    t = Table.query.get_or_404(tid)
    if t.restaurant_id != user.restaurant_id:
        abort(403)
    db.session.delete(t)
    db.session.commit()
    flash("Table deleted.", "success")
    return redirect(url_for("portal.tables"))


@portal_bp.route("/reservations")
def reservations():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant
    if not restaurant:
        abort(404)

    query = Reservation.query.filter_by(restaurant_id=restaurant.id)
    date_str = request.args.get("date")
    if date_str:
        try:
            query = query.filter(Reservation.reservation_date == dt.date.fromisoformat(date_str))
        except:
            pass
    status = request.args.get("status", "all")
    if status != "all":
        query = query.filter(Reservation.status == status)
    reservations_list = query.order_by(Reservation.reservation_date.desc(), Reservation.reservation_time).all()
    return render_template("portal/reservations.html",
                           restaurant=restaurant, reservations=reservations_list, as_money=as_money)


@portal_bp.route("/reservation/<int:res_id>/status", methods=["POST"])
def reservation_status(res_id):
    require_portal_login()
    user = get_portal_user()
    r = Reservation.query.get_or_404(res_id)
    if r.restaurant_id != user.restaurant_id:
        abort(403)

    new_status = request.form.get("status")
    if new_status in ["confirmed", "seated", "no_show", "completed", "cancelled"]:
        old_status = r.status
        r.status = new_status
        if new_status == "no_show" and old_status != "no_show":
            record = NoShowRecord(
                guest_email=r.guest_email.lower().strip(),
                guest_phone=r.guest_phone,
                restaurant_id=r.restaurant_id,
                reservation_id=r.id,
            )
            fee_amount = r.restaurant.no_show_fee_cents
            if fee_amount > 0 and r.stripe_payment_method_id:
                try:
                    ns_kwargs = {
                        "amount": fee_amount,
                        "currency": "usd",
                        "payment_method": r.stripe_payment_method_id,
                        "confirm": True,
                        "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                        "description": f"No-show fee for {r.guest_name} at {r.restaurant.name}",
                    }

                    ns_platform_fee = 0
                    if r.restaurant.stripe_connect_id and r.restaurant.stripe_charges_enabled:
                        ns_platform_fee = int(fee_amount * r.restaurant.platform_fee_percent / 100)
                        ns_kwargs["application_fee_amount"] = ns_platform_fee
                        ns_kwargs["transfer_data"] = {"destination": r.restaurant.stripe_connect_id}

                    pi = stripe.PaymentIntent.create(**ns_kwargs)
                    record.fee_charged_cents = fee_amount
                    r.no_show_fee_charged = True
                    r.no_show_fee_amount_cents = fee_amount

                    ns_txn = PaymentTransaction(
                        reservation_id=r.id,
                        restaurant_id=r.restaurant_id,
                        transaction_type="no_show_fee",
                        amount_cents=fee_amount,
                        platform_fee_cents=ns_platform_fee if r.restaurant.stripe_connect_id and r.restaurant.stripe_charges_enabled else fee_amount,
                        restaurant_amount_cents=fee_amount - ns_platform_fee if r.restaurant.stripe_connect_id and r.restaurant.stripe_charges_enabled else 0,
                        stripe_payment_intent_id=pi.id,
                        status="completed"
                    )
                    db.session.add(ns_txn)
                except Exception as e:
                    print(f"[NO-SHOW FEE ERROR] {e}")
            db.session.add(record)
            send_no_show_email(r, r.restaurant.no_show_fee_cents)
            try:
                send_restaurant_no_show(r, r.restaurant.no_show_fee_cents)
            except Exception as e:
                print(f"Restaurant no-show notification error: {e}")
        db.session.commit()
        flash(f"Reservation marked as {new_status}.", "success")
    return redirect(url_for("portal.reservations"))


@portal_bp.route("/no-show-stats")
def no_show_stats():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant
    if not restaurant:
        abort(404)

    days = int(request.args.get("days", 30))
    since = dt.datetime.utcnow() - dt.timedelta(days=days)

    records = NoShowRecord.query.filter(
        NoShowRecord.restaurant_id == restaurant.id,
        NoShowRecord.occurred_at >= since,
    ).all()

    total_no_shows = len(records)
    total_fees = sum(r.fee_charged_cents for r in records)

    email_counts = {}
    for r in records:
        if r.guest_email not in email_counts:
            email_counts[r.guest_email] = {"count": 0, "total_fees": 0}
        email_counts[r.guest_email]["count"] += 1
        email_counts[r.guest_email]["total_fees"] += r.fee_charged_cents

    repeat_offenders = [
        {"email": email, "count": data["count"], "total_fees": data["total_fees"]}
        for email, data in email_counts.items() if data["count"] >= 2
    ]
    repeat_offenders.sort(key=lambda x: x["count"], reverse=True)

    return render_template("portal/no_show_stats.html",
                           restaurant=restaurant,
                           total_no_shows=total_no_shows,
                           total_fees=total_fees,
                           repeat_offenders=repeat_offenders,
                           as_money=as_money)


@portal_bp.route("/connect-stripe", methods=["POST"])
def connect_stripe():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant

    if not restaurant.stripe_connect_id:
        account = stripe.Account.create(
            type="express",
            country="US",
            email=user.email,
            capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
            business_type="company",
            metadata={"restaurant_id": str(restaurant.id), "restaurant_name": restaurant.name}
        )
        restaurant.stripe_connect_id = account.id
        restaurant.stripe_connect_status = "pending"
        db.session.commit()

    account_link = stripe.AccountLink.create(
        account=restaurant.stripe_connect_id,
        refresh_url=BASE_URL + "/portal/connect-stripe/refresh",
        return_url=BASE_URL + "/portal/connect-stripe/complete",
        type="account_onboarding",
    )
    return redirect(account_link.url)


@portal_bp.route("/connect-stripe/complete")
def connect_stripe_complete():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant

    if restaurant.stripe_connect_id:
        account = stripe.Account.retrieve(restaurant.stripe_connect_id)
        restaurant.stripe_charges_enabled = account.charges_enabled
        restaurant.stripe_payouts_enabled = account.payouts_enabled
        if account.charges_enabled:
            restaurant.stripe_connect_status = "active"
        db.session.commit()

    if restaurant.stripe_charges_enabled:
        try:
            send_restaurant_stripe_connected(restaurant)
        except Exception as e:
            print(f"Stripe connected notification error: {e}")

    flash("Stripe account connected successfully!" if restaurant.stripe_charges_enabled else "Please complete your Stripe setup.", "success" if restaurant.stripe_charges_enabled else "warning")
    return redirect(url_for("portal.dashboard"))


@portal_bp.route("/connect-stripe/refresh")
def connect_stripe_refresh():
    require_portal_login()
    return redirect(url_for("portal.connect_stripe"), code=307)


@portal_bp.route("/stripe-dashboard")
def stripe_dashboard_link():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant
    if restaurant.stripe_connect_id:
        login_link = stripe.Account.create_login_link(restaurant.stripe_connect_id)
        return redirect(login_link.url)
    flash("Please connect your Stripe account first.", "error")
    return redirect(url_for("portal.dashboard"))


@portal_bp.route("/earnings")
def earnings():
    require_portal_login()
    user = get_portal_user()
    restaurant = user.restaurant
    if not restaurant:
        abort(404)

    days = int(request.args.get("days", 30))
    since = dt.datetime.utcnow() - dt.timedelta(days=days)

    transactions = PaymentTransaction.query.filter(
        PaymentTransaction.restaurant_id == restaurant.id,
        PaymentTransaction.created_at >= since,
    ).order_by(PaymentTransaction.created_at.desc()).all()

    total_revenue = sum(t.amount_cents for t in transactions if t.status == "completed" and t.transaction_type != "refund")
    total_platform_fees = sum(t.platform_fee_cents for t in transactions if t.status == "completed" and t.transaction_type != "refund")
    total_restaurant_earnings = sum(t.restaurant_amount_cents for t in transactions if t.status == "completed" and t.transaction_type != "refund")
    total_refunds = sum(t.amount_cents for t in transactions if t.transaction_type == "refund")

    return render_template("portal/earnings.html",
                           restaurant=restaurant,
                           transactions=transactions,
                           total_revenue=total_revenue,
                           total_platform_fees=total_platform_fees,
                           total_restaurant_earnings=total_restaurant_earnings,
                           total_refunds=total_refunds,
                           days=days, as_money=as_money)
