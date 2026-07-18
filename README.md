# ExpiryAlert - Multi-Store Retail Inventory & Consumer Alert System

ExpiryAlert is a modern, professional retail inventory optimization and consumer safety platform built on Flask and Supabase (PostgreSQL). It enables store owners to register batches of stock using unique QR code identifiers, scan products at billing checkout counters, generate receipt invoices, and automatically notify consumers via SMS and Email before their purchased items expire.

---

## Features
- **Multi-Store Management**: Run and monitor multiple storefront branches from a centralized console.
- **QR Product Registry**: Register incoming batches with precise expiry dates and custom QR labels.
- **QR Billing Scanner**: Scans QR codes at cash counters to compile invoices instantly.
- **Consumer Portal**: Customer accounts can check purchase history, warranty periods, and visual expiry timelines.
- **Automated Alerts**: Runs daily background cron processes to send SMS/Email notifications before product expirations.
- **Analytics & Reporting**: Interactive Chart.js graphs displaying daily sales, branch statistics, and wastage logs.

---

## 🛠 Setup & Installation

### 1. Prerequisites
Ensure you have the following installed on your machine:
- **Python 3.8+**
- **PostgreSQL / Supabase Account**

### 2. Clone the Repository & Install Dependencies
```bash
git clone https://github.com/himeshChaudhari/alert-system.git
cd alert-system
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy the template configuration file to `.env`:
```bash
cp .env.example .env
```

Open `.env` and fill out your database settings, API credentials, and default seeding profiles:

```ini
# Database Settings
DATABASE_URL=postgresql://<user>:<password>@<host>:<port>/<dbname>

# Flask Secrets
secret_key=your_flask_secret_key_here
FLASK_ENV=development

# SMTP Email Settings (for sending alerts/invoices)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASS=your_app_password
SMTP_SENDER=your_email@gmail.com
ADMIN_EMAIL=your_admin_email@gmail.com

# SMS API Settings (TextBee)
TEXTBEE_API_KEY=your_textbee_api_key
TEXTBEE_DEVICE_ID=your_textbee_device_id

# Security Auth Token
CRON_SECRET=your_cron_auth_token_here

# Default Seeding Accounts
SUPERADMIN_NAME=Super Admin
SUPERADMIN_PHONE=0000000000
SUPERADMIN_EMAIL=[REDACTED_SUPERADMIN_EMAIL]
SUPERADMIN_PASSWORD=[REDACTED_SUPERADMIN_PASSWORD]

STORE_ADMIN_NAME=Store Admin
STORE_ADMIN_PHONE=1111111111
STORE_ADMIN_EMAIL=[REDACTED_ADMIN_EMAIL]
STORE_ADMIN_PASSWORD=[REDACTED_ADMIN_PASSWORD]

STAFF_NAME=Store Staff
STAFF_PHONE=2222222222
STAFF_EMAIL=[REDACTED_STAFF_EMAIL]
STAFF_PASSWORD=[REDACTED_STAFF_PASSWORD]

CUSTOMER_NAME=Jane Customer
CUSTOMER_PHONE=3333333333
CUSTOMER_EMAIL=[REDACTED_CUSTOMER_EMAIL]
CUSTOMER_PASSWORD=[REDACTED_CUSTOMER_PASSWORD]

CUSTOMER2_NAME=John Doe
CUSTOMER2_PHONE=9876543210
CUSTOMER2_EMAIL=[REDACTED_CUSTOMER2_EMAIL]
CUSTOMER2_PASSWORD=[REDACTED_CUSTOMER2_PASSWORD]
```

---

## 🚀 Running the Application

### 1. Initialize and Seed the Database
Apply the migration schema and populate default store and user records:
```bash
# Push schema migrations
supabase db push

# Seed initial store and users from .env
python seed_supabase_db.py
```

### 2. Start the Development Server
```bash
python app.py
```
Open **`http://127.0.0.1:5000`** in your browser to access the SaaS landing page and log in!
