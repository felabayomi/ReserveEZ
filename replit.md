# ReserveEZ - Restaurant Reservation System

## Overview

ReserveEZ (Reserve Table Easy) is a multi-restaurant reservation platform by Errand Easy company, with comprehensive no-show protection. It enables customers to browse restaurants, check real-time availability, and reserve tables with secure deposits or card holds. Restaurant owners have a full admin dashboard for managing tables, reservations, policies, and tracking no-show statistics.

## User Preferences

Preferred communication style: Simple, everyday language.
Branding: "ReserveEZ" (Reserve Table Easy), an Errand Easy company. All rights reserved.

## System Architecture

### Backend Framework
- **Flask** web application with SQLAlchemy ORM
- **PostgreSQL** database (Neon-backed via Replit)
- **Environment-based configuration** using python-dotenv

### Database Models
- **Restaurant**: Name, slug, cuisine type, address, opening hours (JSON), deposit/cancellation policies, no-show fees
- **Table**: Per-restaurant tables with capacity, type (standard/window/booth/counter/patio/private)
- **Reservation**: Full lifecycle tracking (confirmed/seated/cancelled/no_show/completed), deposit tracking, Stripe payment references
- **WaitlistEntry**: Waitlist with auto-notification when slots open, 2-hour expiration
- **NoShowRecord**: Per-guest no-show history for repeat offender tracking
- **PromoCode**: Flexible codes (percent/flat/free), per-restaurant or global, deposit waiver option
- **NotificationLog**: Email delivery tracking

### No-Show Protection System
- **Credit card holds**: Stripe SetupIntent saves card for later charging
- **Prepaid deposits**: Per-person or flat rate deposits charged via Stripe PaymentIntent
- **Confirmation reminders**: 24-hour and 2-hour email reminders with "Confirm Attendance" button
- **No-show tracking**: Per-guest history; repeat offenders (2+) require higher deposits
- **Cancellation policy**: Configurable cutoff hours; free cancellation before cutoff, late cancel fee after
- **No-show fees**: Automatic charging to saved card when marked as no-show
- **Waitlist system**: Auto-notify waitlisted guests when cancellations/no-shows open slots

### Payment Architecture
- **Stripe integration**: PaymentIntents for deposits, SetupIntents for card holds
- **No-show charging**: Creates new PaymentIntent against saved PaymentMethod
- **Automatic refunds**: Full deposit refund for timely cancellations
- **Promo code discounts**: Can reduce or waive deposits

### Email System (Resend)
- **Booking confirmation**: Details, manage link, calendar export link
- **24-hour reminder**: Confirm attendance button, cancellation link
- **2-hour reminder**: Final reminder with no-show policy warning
- **Cancellation confirmation**: Fee notification if late cancel
- **No-show notification**: Fee charged notice, repeat offender warning
- **Waitlist notification**: Available slot alert with 2-hour claim window

### Authentication & Authorization
- **Simple admin access**: Password-based admin panel (ADMIN_PASSWORD env var)
- **No user accounts**: Guest booking with name + email + phone
- **Secure self-service**: Signed tokens (itsdangerous) for manage/cancel links

### Admin Dashboard
- **Overview**: Today's reservations, upcoming count, no-show stats, waitlist count
- **Restaurant management**: Add/edit restaurants, configure policies and opening hours
- **Table management**: Add/edit/delete tables per restaurant
- **Reservation management**: Filter by date/status, mark seated/no-show/completed
- **No-show statistics**: Repeat offenders, per-restaurant stats, fee collection totals
- **Promo codes**: Create/manage codes, toggle active, track usage
- **Waitlist**: View and manage waitlisted guests

### Cron Endpoints
- `/cron/send-reminders` - Sends 24h and 2h email reminders
- `/cron/process-no-shows` - Auto-marks past reservations as no-show, charges fees
- `/cron/expire-waitlist` - Expires unclaimed waitlist notifications and past entries

## External Dependencies

### Payment Services
- **Stripe**: Deposits, card holds, no-show fee charging, refunds

### Email Services
- **Resend**: All transactional emails (confirmations, reminders, notifications)

### Frontend
- **Bootstrap 5.3.3**: UI framework via CDN
- **Stripe.js**: Client-side card element for secure payment

### Python Libraries
- Flask, Flask-SQLAlchemy, psycopg2-binary, stripe, resend, python-dotenv
- itsdangerous (secure token generation), pytz, gunicorn, qrcode, Pillow

### Integrations (Replit Connectors)
- **Stripe**: Connected via Replit connector - credentials fetched automatically
- **Resend**: Connected via Replit connector - API key and from_email fetched automatically

## Environment Variables
- `DATABASE_URL` - PostgreSQL connection string (auto-configured)
- `STRIPE_WEBHOOK_SECRET` - Stripe webhook signing secret
- `ADMIN_PASSWORD` - Admin panel password (default: "admin")
- `FLASK_SECRET` - Flask session secret key
- Stripe and Resend credentials are managed via Replit integrations (no manual env vars needed)
