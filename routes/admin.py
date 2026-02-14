import datetime as dt
import json
from flask import (Blueprint, render_template, request, redirect, url_for, abort,
                   flash, session)
import stripe
from models import db, Restaurant, Table, Reservation, WaitlistEntry, NoShowRecord, PromoCode, RestaurantUser, PaymentTransaction, RestaurantNomination
from config import ADMIN_PASSWORD, CUISINE_TYPES
from helpers import as_money, make_slug, notify_waitlist_for_slot
from email_service import send_no_show_email

admin_bp = Blueprint("admin", __name__)


def require_admin():
    if not session.get("admin"):
        from werkzeug.exceptions import HTTPException
        raise HTTPException(response=redirect(url_for("admin.login")))


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin.dashboard"))
        flash("Invalid password.", "error")
    return render_template("admin/login.html")


@admin_bp.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("public.index"))


@admin_bp.route("", strict_slashes=False)
def dashboard():
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


@admin_bp.route("/restaurants")
def restaurants():
    require_admin()
    restaurants_list = Restaurant.query.order_by(Restaurant.name).all()
    pending_users = RestaurantUser.query.filter_by(approved=False).all()
    return render_template("admin/restaurants.html", restaurants=restaurants_list, pending_users=pending_users)


@admin_bp.route("/portal-user/<int:uid>/approve", methods=["POST"])
def approve_user(uid):
    require_admin()
    user = RestaurantUser.query.get_or_404(uid)
    user.approved = True
    if user.restaurant:
        user.restaurant.active = True
    db.session.commit()
    flash(f"Approved {user.name}'s restaurant '{user.restaurant.name}'!", "success")
    return redirect(url_for("admin.restaurants"))


@admin_bp.route("/portal-user/<int:uid>/reject", methods=["POST"])
def reject_user(uid):
    require_admin()
    user = RestaurantUser.query.get_or_404(uid)
    restaurant = user.restaurant
    db.session.delete(user)
    if restaurant:
        db.session.delete(restaurant)
    db.session.commit()
    flash("Restaurant submission rejected.", "success")
    return redirect(url_for("admin.restaurants"))


@admin_bp.route("/restaurant/new", methods=["GET", "POST"])
def restaurant_new():
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
            platform_fee_percent=int(request.form.get("platform_fee_percent", 10)),
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
        return redirect(url_for("admin.restaurants"))

    return render_template("admin/restaurant_form.html",
                           restaurant=None, cuisine_types=CUISINE_TYPES)


@admin_bp.route("/restaurant/<int:rid>/toggle-visibility", methods=["POST"])
def toggle_visibility(rid):
    require_admin()
    r = Restaurant.query.get_or_404(rid)
    r.active = not r.active
    db.session.commit()
    flash(f"{r.name} is now {'visible' if r.active else 'hidden'} on the public site.", "success")
    return redirect(url_for("admin.restaurants"))


@admin_bp.route("/restaurant/<int:rid>/edit", methods=["GET", "POST"])
def restaurant_edit(rid):
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
        r.platform_fee_percent = int(request.form.get("platform_fee_percent", 10))
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
        return redirect(url_for("admin.restaurants"))

    return render_template("admin/restaurant_form.html",
                           restaurant=r, cuisine_types=CUISINE_TYPES)


@admin_bp.route("/restaurant/<int:rid>/tables", methods=["GET"])
def tables(rid):
    require_admin()
    restaurant = Restaurant.query.get_or_404(rid)
    tables_list = Table.query.filter_by(restaurant_id=rid).order_by(Table.name).all()
    return render_template("admin/tables.html", restaurant=restaurant, tables=tables_list)


@admin_bp.route("/restaurant/<int:rid>/table/add", methods=["POST"])
def table_add(rid):
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
    return redirect(url_for("admin.tables", rid=rid))


@admin_bp.route("/table/<int:tid>/edit", methods=["POST"])
def table_edit(tid):
    require_admin()
    table = Table.query.get_or_404(tid)
    table.name = request.form.get("name", table.name)
    table.capacity = int(request.form.get("capacity", table.capacity))
    table.table_type = request.form.get("table_type", table.table_type)
    table.active = request.form.get("active") == "on"
    db.session.commit()
    flash("Table updated!", "success")
    return redirect(url_for("admin.tables", rid=table.restaurant_id))


@admin_bp.route("/table/<int:tid>/delete", methods=["POST"])
def table_delete(tid):
    require_admin()
    table = Table.query.get_or_404(tid)
    rid = table.restaurant_id
    db.session.delete(table)
    db.session.commit()
    flash("Table deleted.", "success")
    return redirect(url_for("admin.tables", rid=rid))


@admin_bp.route("/restaurant/<int:rid>/reservations")
def reservations(rid):
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

    reservations_list = query.order_by(Reservation.reservation_time).all()

    return render_template("admin/reservations.html",
                           restaurant=restaurant,
                           reservations=reservations_list,
                           filter_date=filter_date,
                           status_filter=status_filter,
                           as_money=as_money)


@admin_bp.route("/reservation/<int:res_id>/status", methods=["POST"])
def update_status(res_id):
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
                ns_kwargs = {
                    "amount": fee_amount,
                    "currency": "usd",
                    "payment_method": reservation.stripe_payment_method_id,
                    "confirm": True,
                    "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                    "metadata": {"type": "no_show_fee",
                                 "reservation_uuid": reservation.uuid}
                }

                ns_platform_fee = 0
                if reservation.restaurant.stripe_connect_id and reservation.restaurant.stripe_charges_enabled:
                    ns_platform_fee = int(fee_amount * reservation.restaurant.platform_fee_percent / 100)
                    ns_kwargs["application_fee_amount"] = ns_platform_fee
                    ns_kwargs["transfer_data"] = {"destination": reservation.restaurant.stripe_connect_id}

                pi = stripe.PaymentIntent.create(**ns_kwargs)
                reservation.no_show_fee_charged = True
                reservation.no_show_fee_amount_cents = fee_amount
                record.fee_charged_cents = fee_amount

                ns_txn = PaymentTransaction(
                    reservation_id=reservation.id,
                    restaurant_id=reservation.restaurant_id,
                    transaction_type="no_show_fee",
                    amount_cents=fee_amount,
                    platform_fee_cents=ns_platform_fee if reservation.restaurant.stripe_connect_id and reservation.restaurant.stripe_charges_enabled else fee_amount,
                    restaurant_amount_cents=fee_amount - ns_platform_fee if reservation.restaurant.stripe_connect_id and reservation.restaurant.stripe_charges_enabled else 0,
                    stripe_payment_intent_id=pi.id,
                    status="completed"
                )
                db.session.add(ns_txn)
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
    return redirect(url_for("admin.reservations", rid=reservation.restaurant_id,
                            date=reservation.reservation_date.isoformat()))


@admin_bp.route("/no-show-stats")
def no_show_stats():
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


@admin_bp.route("/promo-codes")
def promo_codes():
    require_admin()
    codes = PromoCode.query.order_by(PromoCode.created_at.desc()).all()
    restaurants_list = Restaurant.query.filter_by(active=True).all()
    return render_template("admin/promo_codes.html", codes=codes, restaurants=restaurants_list,
                           as_money=as_money)


@admin_bp.route("/promo-code/add", methods=["POST"])
def promo_code_add():
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
    return redirect(url_for("admin.promo_codes"))


@admin_bp.route("/promo-code/<int:pid>/toggle", methods=["POST"])
def promo_code_toggle(pid):
    require_admin()
    code = PromoCode.query.get_or_404(pid)
    code.active = not code.active
    db.session.commit()
    flash(f"Promo code {'activated' if code.active else 'deactivated'}.", "success")
    return redirect(url_for("admin.promo_codes"))


@admin_bp.route("/waitlist")
def waitlist():
    require_admin()
    entries = WaitlistEntry.query.filter(
        WaitlistEntry.status.in_(["waiting", "notified"])
    ).order_by(WaitlistEntry.desired_date, WaitlistEntry.created_at).all()
    return render_template("admin/waitlist.html", entries=entries, as_money=as_money)


@admin_bp.route("/finances")
def finances():
    require_admin()
    days = int(request.args.get("days", 30))
    since = dt.datetime.utcnow() - dt.timedelta(days=days)

    transactions = PaymentTransaction.query.filter(
        PaymentTransaction.created_at >= since,
    ).order_by(PaymentTransaction.created_at.desc()).all()

    total_volume = sum(t.amount_cents for t in transactions if t.status == "completed" and t.transaction_type != "refund")
    total_platform_revenue = sum(t.platform_fee_cents for t in transactions if t.status == "completed" and t.transaction_type != "refund")
    total_restaurant_payouts = sum(t.restaurant_amount_cents for t in transactions if t.status == "completed" and t.transaction_type != "refund")
    total_refunds = sum(t.amount_cents for t in transactions if t.transaction_type == "refund")

    restaurant_stats = db.session.query(
        Restaurant.name,
        Restaurant.stripe_connect_status,
        db.func.count(PaymentTransaction.id).label("txn_count"),
        db.func.coalesce(db.func.sum(PaymentTransaction.amount_cents), 0).label("total_amount"),
        db.func.coalesce(db.func.sum(PaymentTransaction.platform_fee_cents), 0).label("platform_fees"),
        db.func.coalesce(db.func.sum(PaymentTransaction.restaurant_amount_cents), 0).label("restaurant_earnings"),
    ).join(PaymentTransaction, PaymentTransaction.restaurant_id == Restaurant.id)\
     .filter(PaymentTransaction.created_at >= since, PaymentTransaction.status == "completed")\
     .group_by(Restaurant.id, Restaurant.name, Restaurant.stripe_connect_status).all()

    connected_count = Restaurant.query.filter_by(stripe_connect_status="active").count()
    pending_count = Restaurant.query.filter_by(stripe_connect_status="pending").count()
    not_connected_count = Restaurant.query.filter(Restaurant.stripe_connect_status.in_(["not_connected", None])).count()

    return render_template("admin/finances.html",
                           transactions=transactions,
                           total_volume=total_volume,
                           total_platform_revenue=total_platform_revenue,
                           total_restaurant_payouts=total_restaurant_payouts,
                           total_refunds=total_refunds,
                           restaurant_stats=restaurant_stats,
                           connected_count=connected_count,
                           pending_count=pending_count,
                           not_connected_count=not_connected_count,
                           days=days, as_money=as_money)


@admin_bp.route("/nominations")
def nominations():
    require_admin()
    status_filter = request.args.get("status", "all")
    query = RestaurantNomination.query
    if status_filter != "all":
        query = query.filter_by(status=status_filter)
    noms = query.order_by(RestaurantNomination.created_at.desc()).all()
    counts = {
        "total": RestaurantNomination.query.count(),
        "pending": RestaurantNomination.query.filter_by(status="pending").count(),
        "contacted": RestaurantNomination.query.filter_by(status="contacted").count(),
        "joined": RestaurantNomination.query.filter_by(status="joined").count(),
    }
    return render_template("admin/nominations.html", nominations=noms, status_filter=status_filter, counts=counts)


@admin_bp.route("/nomination/<int:nid>/status", methods=["POST"])
def nomination_status(nid):
    require_admin()
    nom = RestaurantNomination.query.get_or_404(nid)
    new_status = request.form.get("status")
    if new_status in ("pending", "contacted", "joined", "dismissed"):
        nom.status = new_status
        db.session.commit()
        flash(f"Nomination for '{nom.restaurant_name}' marked as {new_status}.", "success")
    return redirect(url_for("admin.nominations"))
