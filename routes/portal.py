import datetime as dt
import json
from flask import (Blueprint, render_template, request, redirect, url_for, abort,
                   flash, session)
import stripe
from models import db, Restaurant, Table, Reservation, NoShowRecord, WaitlistEntry, RestaurantUser
from config import CUISINE_TYPES
from helpers import as_money, make_slug
from email_service import send_no_show_email

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
            if r.restaurant.no_show_fee_cents > 0 and r.stripe_payment_method_id:
                try:
                    pi = stripe.PaymentIntent.create(
                        amount=r.restaurant.no_show_fee_cents,
                        currency="usd",
                        payment_method=r.stripe_payment_method_id,
                        confirm=True,
                        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                        description=f"No-show fee for {r.guest_name} at {r.restaurant.name}",
                    )
                    record.fee_charged_cents = r.restaurant.no_show_fee_cents
                    r.no_show_fee_charged = True
                    r.no_show_fee_amount_cents = r.restaurant.no_show_fee_cents
                except Exception as e:
                    print(f"[NO-SHOW FEE ERROR] {e}")
            db.session.add(record)
            send_no_show_email(r, r.restaurant.no_show_fee_cents)
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
