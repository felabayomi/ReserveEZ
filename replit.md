# EasyDesk Booking System

## Overview

EasyDesk is a flexible desk booking and coworking space management system designed for City Discoverer at 50 Stately St, Suite 2, Wiley Ford WV 26767. The system enables customers to book workspace by the hour or purchase day/week/month passes for unlimited bookings during valid periods. It supports both online payments through Mercury and onsite payments via Chase POS terminal.

The application handles resource management with configurable capacity limits and opening hours, implements a promotional code system (EASYWEEK for 100% off first booking), and provides administrative tools for managing bookings, resources, and payments.

## User Preferences

Preferred communication style: Simple, everyday language.
Simplified calendar interface: Only today shown in olive green, all other dates normal/white until actually booked.

## System Architecture

### Backend Framework
- **Flask** web application with SQLAlchemy ORM for database operations
- **SQLite** database for local development with migration-ready structure
- **Environment-based configuration** using python-dotenv for secrets management

### Database Design
- **Resource model**: Stores workspace details including name, hourly rates, capacity, and JSON-encoded opening hours
- **Booking model**: Tracks individual reservations with timestamps, customer details, and payment status
- **Pass model**: Manages day/week/month passes with validity periods and activation status
- **Payment model**: Provider-agnostic payment tracking supporting Mercury and Chase POS

### Payment Architecture
- **Dual payment system**: Mercury API for online payments and Chase POS for onsite transactions
- **Provider abstraction**: Payment layer designed to be swappable between different payment processors
- **Payment states**: Created, pending, paid, failed, refunded status tracking
- **Webhook support**: Mercury webhook endpoint for automated payment confirmation

### Business Logic
- **Capacity management**: Real-time seat availability checking with configurable limits per resource with visual desk icons
- **Opening hours validation**: JSON-stored weekly schedules with day-specific time slots
- **Pass system**: Three tiers (day/week/month) with automatic free booking privileges during validity
- **Promotional codes**: First-booking discount system with email-based eligibility tracking
- **Visual desk representation**: Interactive desk icons showing available/selected/booked status with real-time updates

### Authentication & Authorization
- **Simple admin access**: Password-based admin panel for resource and booking management
- **No user accounts**: Guest booking system with email-based identification
- **Session management**: Flask sessions for admin authentication

### Feature Flags
- **USE_MERCURY**: Toggle between Mercury API and mock payment flows
- **ALLOW_POS_CHECKOUT**: Enable/disable Chase POS onsite payment option
- **Environment-driven**: Production vs development behavior controlled via environment variables

## External Dependencies

### Payment Services
- **Mercury API**: Online payment processing with invoice generation and webhook notifications
- **Chase POS**: Manual payment processing for onsite transactions with admin reconciliation

### Infrastructure Services
- **QR Code generation**: Python qrcode library for check-in QR codes
- **Email notifications**: SMTP support for booking confirmations and receipts (optional)
- **Calendar integration**: ICS file generation for booking calendar imports

### Development Tools
- **Bootstrap 5.3.3**: Frontend UI framework via CDN
- **Python libraries**: Flask, SQLAlchemy, python-dotenv, qrcode, Pillow, requests, cairosvg
- **Environment management**: .env file configuration for secrets and feature flags
- **PWA support**: Web app manifest and mobile home screen icons (olive green desk theme)

### Email Notifications  
- **SendGrid integration**: Automated booking confirmation emails sent on successful checkout
- **Dual recipients**: Emails sent to both customer and admin (hello@citydiscoverer.ai)
- **Professional branding**: Emails from billing@citydiscoverer.ai with hello@citydiscoverer.ai reply-to
- **Comprehensive details**: HTML emails include booking details, workspace info, timing, and location

### Optional Integrations
- **Calendar exports**: .ics file downloads for individual bookings  
- **Webhook endpoints**: Mercury payment confirmation and status updates