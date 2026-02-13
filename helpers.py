import datetime as dt
from models import db, Table, Reservation, NoShowRecord, WaitlistEntry
from config import TIME_SLOTS_INTERVAL


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


def notify_waitlist_for_slot(restaurant_id, date, time, party_size):
    from email_service import send_waitlist_notification

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
