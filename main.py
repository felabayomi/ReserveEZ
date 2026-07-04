import datetime as dt
import json
import tempfile
from flask import Flask
from config import SECRET_KEY, DATABASE_URI, CUISINE_TYPES
from models import db, init_serializer, Restaurant, Table, PromoCode
from helpers import as_money


def create_app():
    app = Flask(__name__, instance_path=tempfile.gettempdir())
    app.config["SECRET_KEY"] = SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}

    db.init_app(app)
    init_serializer(SECRET_KEY)

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

    from routes.public import public_bp
    from routes.portal import portal_bp
    from routes.admin import admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(portal_bp, url_prefix="/portal")
    app.register_blueprint(admin_bp, url_prefix="/admin")

    return app


app = create_app()


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
            ]
            db.session.add_all(tables_r2)

            r3 = Restaurant(
                name="Casa Bella",
                slug="casa-bella",
                description="Experience the warmth of Italy with our handmade pastas, wood-fired pizzas, and an extensive wine collection curated by our sommelier.",
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
