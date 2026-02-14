import os, requests as http_requests
from dotenv import load_dotenv
import stripe
import resend

load_dotenv()

SECRET_KEY = os.getenv("FLASK_SECRET", "restaurant-reserve-secret-key")
DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///reservations.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
SENDER_NAME = os.getenv("SENDER_NAME", "ReserveEZ")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@reserveez.com")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


def _get_replit_token():
    repl_id = os.getenv("REPL_IDENTITY")
    depl_token = os.getenv("WEB_REPL_RENEWAL")
    if repl_id:
        return "repl " + repl_id
    elif depl_token:
        return "depl " + depl_token
    return None


def _get_connection_settings(connector_name):
    hostname = os.getenv("REPLIT_CONNECTORS_HOSTNAME")
    token = _get_replit_token()
    if not hostname or not token:
        return None
    is_production = os.getenv("REPLIT_DEPLOYMENT") == "1"
    env = "production" if is_production else "development"
    url = f"https://{hostname}/api/v2/connection?include_secrets=true&connector_names={connector_name}&environment={env}"
    try:
        resp = http_requests.get(url, headers={"Accept": "application/json", "X_REPLIT_TOKEN": token}, timeout=10)
        data = resp.json()
        items = data.get("items", [])
        return items[0] if items else None
    except Exception as e:
        print(f"[CONNECTOR] Failed to fetch {connector_name}: {e}")
        return None


def init_stripe():
    conn = _get_connection_settings("stripe")
    if conn and conn.get("settings"):
        s = conn["settings"]
        stripe.api_key = s.get("secret", "")
        return s.get("publishable", ""), s.get("secret", "")
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    return os.getenv("STRIPE_PUBLIC_KEY", ""), os.getenv("STRIPE_SECRET_KEY", "")


def init_resend():
    conn = _get_connection_settings("resend")
    if conn and conn.get("settings"):
        s = conn["settings"]
        resend.api_key = s.get("api_key", "")
        return s.get("from_email", "noreply@reserveez.com")
    resend.api_key = os.getenv("RESEND_API_KEY", "")
    return os.getenv("SENDER_EMAIL", "noreply@tablepilot.io")


STRIPE_PUBLIC_KEY, _stripe_secret = init_stripe()
SENDER_EMAIL = init_resend()

YOUR_DOMAIN = os.getenv('REPLIT_DEV_DOMAIN', os.getenv('REPLIT_DOMAINS', 'localhost:5000').split(',')[0])
BASE_URL = f"https://{YOUR_DOMAIN}"

CUISINE_TYPES = [
    "African", "American", "Italian", "Mexican", "Chinese", "Japanese", "Indian",
    "Jamaican", "Nigerian", "Thai", "French", "Mediterranean", "Korean", "Vietnamese", "Greek",
    "Spanish", "Middle Eastern", "Caribbean", "Brazilian", "Ethiopian",
    "Seafood", "Steakhouse", "Vegetarian/Vegan", "Fusion", "Other"
]

TIME_SLOTS_INTERVAL = 30
