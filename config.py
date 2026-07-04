import os
from dotenv import load_dotenv
import stripe
import resend

load_dotenv()

SECRET_KEY = os.getenv("FLASK_SECRET", "restaurant-reserve-secret-key")
DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:////tmp/reservations.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
SENDER_NAME = os.getenv("SENDER_NAME", "ReserveEZ")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@reserveez.com")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")


def init_stripe():
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    return os.getenv("STRIPE_PUBLIC_KEY", ""), os.getenv("STRIPE_SECRET_KEY", "")


def init_resend():
    resend.api_key = os.getenv("RESEND_API_KEY", "")
    return os.getenv("SENDER_EMAIL", "noreply@reserveez.com")


STRIPE_PUBLIC_KEY, _stripe_secret = init_stripe()
SENDER_EMAIL = init_resend()

BASE_URL = os.getenv("BASE_URL", os.getenv("VERCEL_URL", "http://localhost:5000"))
if BASE_URL and not BASE_URL.startswith(("http://", "https://")):
    BASE_URL = f"https://{BASE_URL}"

CUISINE_TYPES = [
    "African", "American", "Italian", "Mexican", "Chinese", "Japanese", "Indian",
    "Jamaican", "Nigerian", "Thai", "French", "Mediterranean", "Korean", "Vietnamese", "Greek",
    "Spanish", "Middle Eastern", "Caribbean", "Brazilian", "Ethiopian",
    "Seafood", "Steakhouse", "Vegetarian/Vegan", "Fusion", "Other"
]

TIME_SLOTS_INTERVAL = 30
