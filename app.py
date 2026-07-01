import sys
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file, g
from collections import Counter

# Mock MySQLdb module path so existing code references don't crash
class MySQLdbMock:
    class cursors:
        DictCursor = "DictCursor"
sys.modules['MySQLdb'] = MySQLdbMock
sys.modules['MySQLdb.cursors'] = MySQLdbMock.cursors
import MySQLdb.cursors
from werkzeug.security import generate_password_hash, check_password_hash
import os
import datetime
import qrcode
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
import requests
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

base_dir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(base_dir, '.env'))

app = Flask(__name__)

# CSRF Protection
csrf = CSRFProtect(app)

# Rate Limiter
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://",
    default_limits=[]
)

# Secret Key Hardening
sec_key = os.environ.get('secret_key')
if not sec_key:
    if os.environ.get('FLASK_ENV') == 'production':
        raise RuntimeError("Critical configuration error: secret_key is required in production environment.")
    else:
        sec_key = 'fallback_secret_key'
app.secret_key = sec_key

# Session Cookie Hardening
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Connection compatibility wrappers for PostgreSQL
class DictCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor
        self._lastrowid = None

    def execute(self, query, vars=None):
        query = query.replace('`', '"')
        
        # Intercept INSERT queries to append RETURNING id to mimic lastrowid
        q_strip = query.strip().upper()
        if q_strip.startswith("INSERT INTO "):
            if "RETURNING" not in q_strip:
                query_stripped = query.rstrip().rstrip(';')
                query = f"{query_stripped} RETURNING id"
                self._cursor.execute(query, vars)
                try:
                    row = self._cursor.fetchone()
                    if row:
                        if isinstance(row, dict):
                            self._lastrowid = row.get('id')
                        else:
                            self._lastrowid = row[0]
                except Exception:
                    pass
                return

        self._cursor.execute(query, vars)

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        return self._cursor.close()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class ConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, cursor_factory=None):
        if cursor_factory is not None:
            # Pymysql passes DictCursor, which we map to RealDictCursor
            raw_cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            raw_cursor = self._conn.cursor()
        return DictCursorWrapper(raw_cursor)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


# Database Configuration
app.config['DATABASE_URL'] = os.environ.get('DATABASE_URL')
app.config['TEXTBEE_API_KEY'] = os.environ.get('TEXTBEE_API_KEY')
app.config['TEXTBEE_DEVICE_ID'] = os.environ.get('TEXTBEE_DEVICE_ID')

class PostgreSQL:
    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        @app.teardown_appcontext
        def teardown_db(exception):
            db = g.pop('pg_db', None)
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    @property
    def connection(self):
        if 'pg_db' not in g:
            from flask import current_app
            db_url = current_app.config['DATABASE_URL']
            if not db_url:
                # Provide a local fallback for development purposes
                db_url = "postgresql://postgres:postgres@localhost:5432/expiry_system"
            conn = psycopg2.connect(db_url)
            g.pg_db = ConnectionWrapper(conn)
        return g.pg_db

# Keep instance variable named mysql to ensure drop-in compatibility
mysql = PostgreSQL(app)

# Email Settings (Modify these for real SMTP servers like Gmail, Mailtrap, etc.)
SMTP_HOST = os.environ.get('SMTP_HOST')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASS = os.environ.get('SMTP_PASS')
SMTP_SENDER = os.environ.get('SMTP_SENDER')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')

# Create email logs directory to store mock email text
EMAIL_LOGS_DIR = os.path.join(base_dir, 'logs')
os.makedirs(EMAIL_LOGS_DIR, exist_ok=True)
EMAIL_LOG_FILE = os.path.join(EMAIL_LOGS_DIR, 'emails.log')
SMS_LOG_FILE = os.path.join(EMAIL_LOGS_DIR, 'sms.log')

# Helper function to send email alerts with a fallback logger
def send_email_alert(subject, body, recipient_email):
    """
    Sends an email using smtplib. If the connection fails (e.g. SMTP server is not running),
    it logs the email to standard console output so the user can verify the email content.
    """
    msg = MIMEMultipart()
    msg['From'] = SMTP_SENDER
    msg['To'] = recipient_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))
    
    email_sent = False
    error_msg = ""
    
    # Try sending via real SMTP
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            if SMTP_PORT == 587:
                server.starttls()
                
        if SMTP_USER and SMTP_PASS:
            server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_SENDER, recipient_email, msg.as_string())
        server.quit()
        email_sent = True
        print(f"[SMTP] Email alert sent successfully to {recipient_email}")
    except Exception as e:
        error_msg = str(e)
        print(f"[SMTP WARNING] Failed to send email via SMTP to {recipient_email}: {e}")
        
    # Log to console stdout (for testing and debugging on Vercel) and write to log file
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_block = f"\n[EMAIL LOG] {'='*50}\nTimestamp: {timestamp}\nTo: {recipient_email}\nSubject: {subject}\nSMTP Status: {'Sent' if email_sent else 'Failed (' + error_msg + ') - Logged Mock Email'}\n{'-'*50}\n{body}\n{'='*50}\n"
        print(log_block)
        with open(EMAIL_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_block)
    except Exception as log_error:
        print(f"[ERROR] Failed to output email log: {log_error}")
        
    return email_sent

# Helper function to send SMS alerts using Textbee API with a fallback logger
def send_sms_alert(phone_number, message):
    """
    Sends an SMS using the Textbee REST API (POST).
    If the credentials are not configured, it logs the message content to standard console output.
    """
    api_key = app.config.get('TEXTBEE_API_KEY')
    device_id = app.config.get('TEXTBEE_DEVICE_ID')
    
    sms_sent = False
    status_msg = ""
    
    # Check if a real key and device id are provided
    if api_key and device_id and api_key != 'mock_api_key' and device_id != 'mock_device_id':
        # Normalize recipient number
        to_number = phone_number.strip()
        cleaned_to = ''.join(c for c in to_number if c.isdigit() or c == '+')
        if not cleaned_to.startswith('+'):
            if len(cleaned_to) == 10:
                cleaned_to = f"+91{cleaned_to}"
            elif len(cleaned_to) == 12 and cleaned_to.startswith('91'):
                cleaned_to = f"+{cleaned_to}"
            else:
                cleaned_to = f"+{cleaned_to}"
                
        url = f"https://api.textbee.dev/api/v1/gateway/devices/{device_id}/send-sms"
        payload = {
            'recipients': [cleaned_to],
            'message': message
        }
        headers = {
            'x-api-key': api_key,
            'Content-Type': 'application/json'
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code in (200, 201):
                sms_sent = True
                print(f"[SMS] Textbee SMS alert sent successfully to {cleaned_to}")
            else:
                try:
                    res_json = response.json()
                    status_msg = f"Textbee API Error: {res_json}"
                except Exception:
                    status_msg = f"HTTP Error {response.status_code}"
                
                # Check for mock credential override in test environments
                if "mock" in api_key.lower() or "mock" in device_id.lower():
                    status_msg = "No API Key configured - Logged Mock SMS"
                    
                print(f"[SMS WARNING] Textbee returned error: {status_msg}")
        except Exception as e:
            status_msg = str(e)
            print(f"[SMS WARNING] Failed to send SMS via Textbee to {cleaned_to}: {e}")
    else:
        status_msg = "No API Key configured - Logged Mock SMS"
        print(f"[SMS INFO] Textbee credentials not configured. Logging mock SMS to console.")
        
    # Log to console stdout (for testing and debugging on Vercel) and write to log file
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_block = f"\n[SMS LOG] {'='*50}\nTimestamp: {timestamp}\nTo: {phone_number}\nSMS Status: {'Sent' if sms_sent else 'Failed (' + status_msg + ')'}\n{'-'*50}\n{message}\n{'='*50}\n"
        print(log_block)
        with open(SMS_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_block)
    except Exception as log_error:
        print(f"[ERROR] Failed to output SMS log: {log_error}")
        
    return sms_sent


# Authentication Decorators
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def role_required(allowed_roles):
    roles = list(allowed_roles)
    if 'admin' in roles and 'super_admin' not in roles:
        roles.append('super_admin')
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session or session['user_role'] not in roles:
                flash('Unauthorized access!', 'danger')
                return redirect(url_for('dashboard_router'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Routing logic
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard_router'))
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard_router():
    """Redirects the user to the correct dashboard based on their role."""
    role = session.get('user_role')
    if role == 'super_admin':
        return redirect(url_for('superadmin_dashboard'))
    elif role == 'admin':
        return redirect(url_for('admin_dashboard'))
    elif role == 'staff':
        return redirect(url_for('staff_dashboard'))
    elif role == 'customer':
        return redirect(url_for('customer_dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard_router'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        cur.close()
        
        if user and check_password_hash(user['password'], password):
            # Check if user account is deactivated (for staff and admin roles)
            if user['role'] in ['admin', 'staff']:
                if not user.get('is_active', True):
                    flash('Your account has been deactivated. Please contact your store administrator.', 'danger')
                    return render_template('login.html')
                    
                # Check if store is active
                if user['store_id']:
                    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                    cur.execute("SELECT is_active FROM stores WHERE id = %s", (user['store_id'],))
                    store = cur.fetchone()
                    cur.close()
                    if store and not store['is_active']:
                        flash('Your store account has been suspended. Contact support.', 'danger')
                        return render_template('login.html')
 
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['user_email'] = user['email']
            session['user_role'] = user['role']
            session['store_id'] = user['store_id']
            flash(f"Welcome back, {user['name']}!", 'success')
            return redirect(url_for('dashboard_router'))
        else:
            flash('Invalid email or password.', 'danger')
            
    return render_template('login.html')
 
@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def register():
    if 'user_id' in session:
        return redirect(url_for('dashboard_router'))
        
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        password = request.form.get('password')
        
        # Public registration strictly creates customer accounts
        role = 'customer'
        store_id = None
            
        hashed_password = generate_password_hash(password)
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            # 1. Check if user with this phone number already exists
            cur.execute("SELECT * FROM users WHERE phone = %s", (phone,))
            existing_user = cur.fetchone()
            
            if existing_user:
                # If they exist and have NO email, they were auto-created at billing counter
                if not existing_user['email']:
                    cur.execute(
                        "UPDATE users SET name = %s, email = %s, password = %s, role = %s, store_id = %s WHERE phone = %s",
                        (name, email, hashed_password, role, store_id, phone)
                    )
                    mysql.connection.commit()
                    flash('Registration completed successfully! Your billing history has been linked.', 'success')
                    return redirect(url_for('login'))
                else:
                    flash('This phone number is already registered to another account.', 'danger')
                    return render_template('register.html')
            
            # 2. If phone doesn't exist, proceed with standard registration
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                flash('Email already registered.', 'danger')
                return render_template('register.html')
                
            cur.execute(
                "INSERT INTO users (name, phone, email, password, role, store_id) VALUES (%s, %s, %s, %s, %s, %s)",
                (name, phone, email, hashed_password, role, store_id)
            )
            mysql.connection.commit()
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            mysql.connection.rollback()
            print(f"[ERROR] Registration failed: {e}")
            flash('Something went wrong. Please try again or contact support.', 'danger')
        finally:
            cur.close()
            
    return render_template('register.html')

@app.route('/store/register', methods=['GET', 'POST'])
def store_register():
    if 'user_id' in session and session.get('user_role') != 'super_admin':
        return redirect(url_for('dashboard_router'))
        
    if request.method == 'POST':
        store_name = request.form.get('store_name')
        store_address = request.form.get('store_address')
        admin_name = request.form.get('admin_name')
        admin_email = request.form.get('admin_email')
        admin_phone = request.form.get('admin_phone')
        admin_password = request.form.get('admin_password')
        
        if not all([store_name, admin_name, admin_email, admin_phone, admin_password]):
            flash('All fields are required.', 'danger')
            return render_template('store_register.html')
            
        hashed_password = generate_password_hash(admin_password)
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            # Check if user with this email already exists
            cur.execute("SELECT id FROM users WHERE email = %s", (admin_email,))
            if cur.fetchone():
                flash('An account with this email address already exists.', 'danger')
                return render_template('store_register.html')
                
            # 1. Insert store
            cur.execute(
                "INSERT INTO stores (name, address, owner_email, is_active) VALUES (%s, %s, %s, TRUE)",
                (store_name, store_address, admin_email)
            )
            store_id = cur.lastrowid
            
            # 2. Insert admin user
            cur.execute(
                "INSERT INTO users (name, phone, email, password, role, store_id) VALUES (%s, %s, %s, %s, 'admin', %s)",
                (admin_name, admin_phone, admin_email, hashed_password, store_id)
            )
            
            mysql.connection.commit()
            if session.get('user_role') == 'super_admin':
                flash(f"Store '{store_name}' and admin account registered successfully!", 'success')
                return redirect(url_for('superadmin_dashboard'))
            else:
                flash('Store and admin account registered successfully! You can now log in.', 'success')
                return redirect(url_for('login'))
        except Exception as e:
            mysql.connection.rollback()
            print(f"[ERROR] Store registration failed: {e}")
            flash('Something went wrong. Please try again or contact support.', 'danger')
        finally:
            cur.close()
            
    return render_template('store_register.html')

@app.route('/admin/register-staff', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def register_staff():
    stores = []
    # If super_admin, they need a list of stores to assign staff to
    if session.get('user_role') == 'super_admin':
        cur_stores = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur_stores.execute("SELECT id, name FROM stores ORDER BY name ASC")
        stores = cur_stores.fetchall()
        cur_stores.close()

    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        password = request.form.get('password')
        
        # Decide which store_id to use
        if session.get('user_role') == 'super_admin':
            store_id = request.form.get('store_id')
            if not store_id:
                flash('Please select a store to assign staff to.', 'danger')
                return render_template('register_staff.html', stores=stores)
            store_id = int(store_id)
        else:
            store_id = session.get('store_id')
            
        if not all([name, email, phone, password, store_id]):
            flash('All fields are required.', 'danger')
            return render_template('register_staff.html', stores=stores)
            
        hashed_password = generate_password_hash(password)
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            # Check if email is already registered
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                flash('Email already registered.', 'danger')
                return render_template('register_staff.html', stores=stores)
                
            # If user with this phone number already exists, see if it was auto-created
            cur.execute("SELECT * FROM users WHERE phone = %s", (phone,))
            existing_user = cur.fetchone()
            if existing_user:
                if not existing_user['email']:
                    cur.execute(
                        "UPDATE users SET name = %s, email = %s, password = %s, role = 'staff', store_id = %s WHERE phone = %s",
                        (name, email, hashed_password, store_id, phone)
                    )
                    mysql.connection.commit()
                    flash(f'Staff member {name} registered successfully (billing history linked)!', 'success')
                    return redirect(url_for('dashboard_router'))
                else:
                    flash('This phone number is already registered to another account.', 'danger')
                    return render_template('register_staff.html', stores=stores)
                    
            cur.execute(
                "INSERT INTO users (name, phone, email, password, role, store_id) VALUES (%s, %s, %s, %s, 'staff', %s)",
                (name, phone, email, hashed_password, store_id)
            )
            mysql.connection.commit()
            flash(f'Staff member {name} registered successfully!', 'success')
            return redirect(url_for('dashboard_router'))
        except Exception as e:
            mysql.connection.rollback()
            print(f"[ERROR] Staff registration failed: {e}")
            flash('Something went wrong. Please try again or contact support.', 'danger')
        finally:
            cur.close()
            
    return render_template('register_staff.html', stores=stores)

@app.route('/admin/create-staff', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def create_staff():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        password = request.form.get('password')
        
        # Admin sets the initial password explicitly on the form
        store_id = session.get('store_id')
        if not store_id:
            flash('Error: Store context missing. Super admin cannot create store staff directly.', 'danger')
            return redirect(url_for('dashboard_router'))
            
        if not all([name, email, phone, password]):
            flash('All fields are required.', 'danger')
            return render_template('create_staff.html')
            
        hashed_password = generate_password_hash(password)
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            # Check if email is already registered
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                flash('Email already registered.', 'danger')
                return render_template('create_staff.html')
                
            # Check if user with this phone number already exists
            cur.execute("SELECT * FROM users WHERE phone = %s", (phone,))
            existing_user = cur.fetchone()
            if existing_user:
                if not existing_user['email']:
                    # Update existing customer placeholder to be a staff member
                    cur.execute(
                        "UPDATE users SET name = %s, email = %s, password = %s, role = 'staff', store_id = %s, is_active = TRUE WHERE phone = %s",
                        (name, email, hashed_password, store_id, phone)
                    )
                    mysql.connection.commit()
                    flash(f'Staff member {name} created successfully (linked billing history)!', 'success')
                    return redirect(url_for('admin_dashboard'))
                else:
                    flash('This phone number is already registered to another account.', 'danger')
                    return render_template('create_staff.html')
                    
            # Insert new staff member
            cur.execute(
                "INSERT INTO users (name, phone, email, password, role, store_id, is_active) VALUES (%s, %s, %s, %s, 'staff', %s, TRUE)",
                (name, phone, email, hashed_password, store_id)
            )
            mysql.connection.commit()
            flash(f'Staff member {name} created successfully!', 'success')
            return redirect(url_for('admin_dashboard'))
        except Exception as e:
            mysql.connection.rollback()
            print(f"[ERROR] Failed to create staff member: {e}")
            flash('Something went wrong. Please try again or contact support.', 'danger')
        finally:
            cur.close()
            
    return render_template('create_staff.html')

@app.route('/admin/toggle-staff/<int:staff_id>', methods=['POST'])
@login_required
@role_required(['admin'])
def toggle_staff(staff_id):
    store_id = session.get('store_id')
    if not store_id:
        flash('Unauthorized store action.', 'danger')
        return redirect(url_for('dashboard_router'))
        
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        # Verify staff member exists and belongs to the same store
        cur.execute("SELECT id, name, is_active FROM users WHERE id = %s AND store_id = %s AND role = 'staff'", (staff_id, store_id))
        staff = cur.fetchone()
        if not staff:
            flash('Staff member not found or does not belong to your store.', 'danger')
            return redirect(url_for('admin_dashboard'))
            
        new_status = not staff['is_active']
        cur.execute("UPDATE users SET is_active = %s WHERE id = %s", (new_status, staff_id))
        mysql.connection.commit()
        
        status_str = "activated" if new_status else "deactivated"
        flash(f"Staff member '{staff['name']}' has been {status_str}.", 'success')
    except Exception as e:
        mysql.connection.rollback()
        print(f"[ERROR] Failed to update staff status: {e}")
        flash('Something went wrong. Please try again or contact support.', 'danger')
    finally:
        cur.close()
        
    return redirect(url_for('admin_dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('login'))

# ================= STAFF FUNCTIONS =================

@app.route('/staff/dashboard')
@login_required
@role_required(['staff', 'admin'])
def staff_dashboard():
    store_id = request.args.get('store_id', type=int) if session.get('user_role') == 'super_admin' else session.get('store_id')
    is_global = (session.get('user_role') == 'super_admin' and store_id is None) or (session.get('user_role') == 'admin' and session.get('store_id') is None)
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    # Note: 3-day cleanup clears stock, 7-day display filter removes expired items from recent-activity view.
    if is_global:
        cur.execute("""
            SELECT * FROM products 
            WHERE expiry_date >= (CURRENT_DATE - 7) 
            ORDER BY id DESC
        """)
    else:
        cur.execute("""
            SELECT * FROM products 
            WHERE store_id = %s AND expiry_date >= (CURRENT_DATE - 7) 
            ORDER BY id DESC
        """, (store_id,))
    products = cur.fetchall()
    cur.close()
    return render_template('staff_dashboard.html', products=products)

@app.route('/staff/product-price/<int:product_id>')
@login_required
@role_required(['staff', 'admin'])
def get_product_price(product_id):
    """API endpoint: returns product name & price for the billing cart."""
    store_id = session.get('store_id')
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    is_global = session.get('user_role') == 'super_admin' or (session.get('user_role') == 'admin' and store_id is None)
    if is_global:
        cur.execute("SELECT id, name, price_per_pack, stock_quantity, pack_size, unit FROM products WHERE id = %s", (product_id,))
    else:
        cur.execute("SELECT id, name, price_per_pack, stock_quantity, pack_size, unit FROM products WHERE id = %s AND store_id = %s", (product_id, store_id))
    product = cur.fetchone()
    cur.close()
    if product:
        return jsonify({
            'success': True,
            'product': {
                'id': product['id'],
                'name': product['name'],
                'price': float(product['price_per_pack']),
                'stock_quantity': product['stock_quantity'],
                'pack_size': float(product['pack_size']),
                'unit': product['unit']
            }
        })
    return jsonify({'success': False, 'message': 'Product not found.'})

@app.route('/product/qr/<int:product_id>')
@login_required
def get_product_qr(product_id):
    """Generates the QR code image on the fly and streams it as a PNG response."""
    import io
    store_id = session.get('store_id')
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    is_global = session.get('user_role') in ['customer', 'super_admin'] or (session.get('user_role') == 'admin' and store_id is None)
    if is_global:
        cur.execute("SELECT qr_code_data FROM products WHERE id = %s", (product_id,))
    else:
        cur.execute("SELECT qr_code_data FROM products WHERE id = %s AND store_id = %s", (product_id, store_id))
    product = cur.fetchone()
    cur.close()
    
    if not product or not product['qr_code_data']:
        return "QR code not found", 404
        
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(product['qr_code_data'])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    img_io = io.BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    
    return send_file(img_io, mimetype='image/png')

@app.route('/staff/register-product', methods=['POST'])
@login_required
@role_required(['staff', 'admin'])
def register_product():
    name = request.form.get('name')
    expiry_date_str = request.form.get('expiry_date')
    stock_qty_str = request.form.get('stock_quantity', '0')
    pack_size_str = request.form.get('pack_size', '1.00')
    unit = request.form.get('unit', 'piece')
    price_per_pack_str = request.form.get('price_per_pack', '0')
    
    if not name or not expiry_date_str:
        flash('Product name and expiry date are required!', 'danger')
        return redirect(url_for('staff_dashboard'))
    
    # Validate unit
    supported_units = ['litre', 'ml', 'kg', 'gram', 'piece', 'packet', 'bottle', 'box', 'dozen']
    if unit not in supported_units:
        flash('Unsupported unit specified.', 'danger')
        return redirect(url_for('staff_dashboard'))
        
    # Validate stock quantity
    try:
        stock_quantity = int(stock_qty_str)
        if stock_quantity < 0:
            flash('Stock quantity cannot be negative.', 'danger')
            return redirect(url_for('staff_dashboard'))
    except (ValueError, TypeError):
        flash('Invalid stock quantity value.', 'danger')
        return redirect(url_for('staff_dashboard'))
        
    # Validate pack size
    try:
        pack_size = float(pack_size_str)
        if pack_size <= 0:
            flash('Pack size must be greater than zero.', 'danger')
            return redirect(url_for('staff_dashboard'))
    except (ValueError, TypeError):
        flash('Invalid pack size value.', 'danger')
        return redirect(url_for('staff_dashboard'))
        
    # Validate price
    try:
        price_per_pack = float(price_per_pack_str)
        if price_per_pack < 0:
            flash('Price per pack cannot be negative.', 'danger')
            return redirect(url_for('staff_dashboard'))
    except (ValueError, TypeError):
        flash('Invalid price value.', 'danger')
        return redirect(url_for('staff_dashboard'))
        
    try:
        expiry_date = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    except ValueError:
        flash('Invalid date format.', 'danger')
        return redirect(url_for('staff_dashboard'))
        
    staff_id = session['user_id']
    store_id = session.get('store_id')
    
    if not store_id:
        flash('Super admin cannot register products directly. Please use a store-specific staff/admin account.', 'danger')
        return redirect(url_for('staff_dashboard'))
    
    cur = mysql.connection.cursor()
    try:
        # 1. Insert product into DB with price (temporarily without QR code data)
        cur.execute(
            "INSERT INTO products (name, expiry_date, price_per_pack, stock_quantity, pack_size, unit, qr_code_data, registered_by, store_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (name, expiry_date, price_per_pack, stock_quantity, pack_size, unit, "", staff_id, store_id)
        )
        product_id = cur.lastrowid
        
        # 2. Generate QR code payload (e.g. EXP:id:YYYY-MM-DD)
        qr_data = f"EXP:{product_id}:{expiry_date_str}"
        
        # 3. Update the product record with the QR code data
        cur.execute(
            "UPDATE products SET qr_code_data = %s WHERE id = %s",
            (qr_data, product_id)
        )
        mysql.connection.commit()
        
        flash(f'Product "{name}" ({pack_size} {unit} Pack, ₹{price_per_pack:.2f}, Stock: {stock_quantity}) registered successfully!', 'success')
    except Exception as e:
        mysql.connection.rollback()
        print(f"[ERROR] Product registration failed: {e}")
        flash('Something went wrong. Please try again or contact support.', 'danger')
    finally:
        cur.close()
        
    return redirect(url_for('staff_dashboard'))

@app.route('/staff/restock/<int:product_id>', methods=['POST'])
@login_required
@role_required(['staff', 'admin'])
def restock_product(product_id):
    quantity_str = request.form.get('quantity')
    expiry_date_str = request.form.get('expiry_date')
    price_per_pack_str = request.form.get('price_per_pack')
    
    if not quantity_str or not expiry_date_str:
        flash('Quantity and expiry date are required!', 'danger')
        return redirect(url_for('staff_dashboard'))
        
    try:
        quantity = int(quantity_str)
        if quantity <= 0:
            flash('Restock quantity must be greater than zero.', 'danger')
            return redirect(url_for('staff_dashboard'))
    except (ValueError, TypeError):
        flash('Invalid quantity value.', 'danger')
        return redirect(url_for('staff_dashboard'))
        
    try:
        submitted_expiry_date = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    except ValueError:
        flash('Invalid expiry date format. Please use YYYY-MM-DD.', 'danger')
        return redirect(url_for('staff_dashboard'))
        
    new_price = None
    if price_per_pack_str is not None and price_per_pack_str.strip() != '':
        try:
            new_price = float(price_per_pack_str)
            if new_price < 0:
                flash('Price per pack cannot be negative.', 'danger')
                return redirect(url_for('staff_dashboard'))
        except (ValueError, TypeError):
            flash('Invalid price value.', 'danger')
            return redirect(url_for('staff_dashboard'))

    store_id = session.get('store_id')
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        is_global = session.get('user_role') == 'super_admin' or (session.get('user_role') == 'admin' and store_id is None)
        if is_global:
            cur.execute("""
                SELECT id, name, price_per_pack, pack_size, unit, stock_quantity, expiry_date, store_id 
                FROM products WHERE id = %s
            """, (product_id,))
        else:
            cur.execute("""
                SELECT id, name, price_per_pack, pack_size, unit, stock_quantity, expiry_date, store_id 
                FROM products WHERE id = %s AND store_id = %s
            """, (product_id, store_id))
        product = cur.fetchone()
        
        if not product:
            flash('Product not found or access denied.', 'danger')
            return redirect(url_for('staff_dashboard'))
            
        if new_price is None:
            new_price = product['price_per_pack']
            
        db_expiry_date = product['expiry_date']
        
        # Check if the submitted expiry date matches the database expiry date
        if db_expiry_date == submitted_expiry_date:
            # Match: simply increment stock_quantity on the existing row and update the price
            new_stock = product['stock_quantity'] + quantity
            # Note: Changing price_per_pack going forward does not retroactively affect any past bills since unit_price is snapshotted at time of sale.
            cur.execute("UPDATE products SET stock_quantity = %s, price_per_pack = %s WHERE id = %s", (new_stock, new_price, product_id))
            mysql.connection.commit()
            
            formatted_date = submitted_expiry_date.strftime("%d %B") if submitted_expiry_date else 'N/A'
            flash(f"Restocked existing batch — new total: {new_stock} units (exp. {formatted_date}). Price updated to ₹{new_price:.2f}/pack.", 'success')
        else:
            # Mismatch: create a brand-new product row with the same attributes but new expiry, quantity & price
            staff_id = session['user_id']
            cur.execute("""
                INSERT INTO products (name, expiry_date, price_per_pack, stock_quantity, pack_size, unit, qr_code_data, registered_by, store_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                product['name'],
                submitted_expiry_date,
                new_price,
                quantity,
                product['pack_size'],
                product['unit'],
                "",  # temporary qr_code_data
                staff_id,
                product['store_id']
            ))
            new_product_id = cur.lastrowid
            
            # Generate and save new qr_code_data
            new_qr_data = f"EXP:{new_product_id}:{expiry_date_str}"
            cur.execute("UPDATE products SET qr_code_data = %s WHERE id = %s", (new_qr_data, new_product_id))
            mysql.connection.commit()
            
            old_expiry_formatted = db_expiry_date.strftime("%d %B") if db_expiry_date else 'N/A'
            new_expiry_formatted = submitted_expiry_date.strftime("%d %B") if submitted_expiry_date else 'N/A'
            
            flash(
                f"New batch detected — created new product entry with a fresh QR code (exp. {new_expiry_formatted}) at ₹{new_price:.2f}/pack. "
                f"Old batch ({product['stock_quantity']} units, exp. {old_expiry_formatted}) remains active at ₹{product['price_per_pack']:.2f}/pack until sold out.", 
                'success'
            )
            
    except Exception as e:
        mysql.connection.rollback()
        print(f"[ERROR] Product restock failed: {e}")
        flash('Something went wrong. Please try again or contact support.', 'danger')
    finally:
        cur.close()
        
    return redirect(url_for('staff_dashboard'))

@app.route('/staff/billing')
@login_required
@role_required(['staff', 'admin'])
def staff_billing():
    return render_template('staff_billing.html')

@app.route('/staff/lookup-customer')
@login_required
@role_required(['staff', 'admin'])
def lookup_customer():
    phone = request.args.get('phone')
    if not phone:
        return jsonify({'success': False, 'message': 'Phone number is required.'})
        
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT id, name, email FROM users WHERE phone = %s AND role = 'customer'", (phone,))
    customer = cur.fetchone()
    cur.close()
    
    if customer:
        return jsonify({'success': True, 'customer': customer})
    else:
        return jsonify({'success': False, 'message': 'Customer not found.'})

@app.route('/staff/create-customer', methods=['POST'])
@login_required
@role_required(['staff', 'admin'])
def create_customer():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request.'})
        
    name = data.get('name')
    phone = data.get('phone')
    
    if not name or not phone:
        return jsonify({'success': False, 'message': 'Name and phone are required.'})
        
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        # Check if phone number already exists
        cur.execute("SELECT id, name, email FROM users WHERE phone = %s", (phone,))
        existing = cur.fetchone()
        if existing:
            return jsonify({'success': True, 'customer': existing, 'message': 'Customer already exists.'})
            
        # Create new customer with NULL email and password
        cur.execute(
            "INSERT INTO users (name, phone, email, password, role) VALUES (%s, %s, NULL, NULL, 'customer')",
            (name, phone)
        )
        mysql.connection.commit()
        customer_id = cur.lastrowid
        
        return jsonify({
            'success': True,
            'customer': {
                'id': customer_id,
                'name': name,
                'email': None
            },
            'message': 'Customer registered successfully!'
        })
    except Exception as e:
        mysql.connection.rollback()
        print(f"[ERROR] Failed to create customer: {e}")
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again or contact support.'})
    finally:
        cur.close()

@app.route('/staff/checkout', methods=['POST'])
@login_required
@role_required(['staff', 'admin'])
def checkout():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid request data.'})
        
    customer_id = data.get('customer_id')
    product_ids = data.get('product_ids', []) # List of product IDs (may contain duplicates)
    
    if not customer_id or not product_ids:
        return jsonify({'success': False, 'message': 'Missing customer ID or products.'})
    
    # Validate: at least one item
    if len(product_ids) == 0:
        return jsonify({'success': False, 'message': 'Cannot create an empty bill.'})
        
    # Count quantity of each product (e.g. [1, 1, 2] -> {1: 2, 2: 1})
    product_quantities = Counter(product_ids)
    
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        today = datetime.date.today()
        now = datetime.datetime.now()
        staff_id = session['user_id']
        staff_store_id = session.get('store_id')
        if not staff_store_id:
            return jsonify({'success': False, 'message': 'Super admin cannot process checkouts directly. Please use a store-specific account.'})
        
        # Fetch prices and stock for all products in this transaction
        prod_id_list = list(product_quantities.keys())
        format_strings = ','.join(['%s'] * len(prod_id_list))
        cur.execute(f"SELECT id, name, price_per_pack, stock_quantity FROM products WHERE id IN ({format_strings}) AND store_id = %s", tuple(prod_id_list + [staff_store_id]))
        products_data = {row['id']: row for row in cur.fetchall()}
        
        # Validate stock levels and calculate grand total
        grand_total = 0.0
        for prod_id, qty in product_quantities.items():
            if prod_id not in products_data:
                return jsonify({'success': False, 'message': f'Product ID {prod_id} not found.'})
            
            available_stock = products_data[prod_id]['stock_quantity']
            if available_stock < qty:
                return jsonify({
                    'success': False,
                    'message': f"Insufficient stock for product '{products_data[prod_id]['name']}'. Available: {available_stock}, Requested: {qty}."
                })
                
            unit_price = float(products_data[prod_id]['price_per_pack'])
            grand_total += unit_price * qty
        
        # 1. Create the bill record
        cur.execute(
            "INSERT INTO bills (customer_id, staff_id, total_amount, bill_date, store_id) VALUES (%s, %s, %s, %s, %s)",
            (customer_id, staff_id, grand_total, now, staff_store_id)
        )
        bill_id = cur.lastrowid
        
        # 2. Insert purchase rows linked to the bill and decrement product stock
        for prod_id, qty in product_quantities.items():
            unit_price = float(products_data[prod_id]['price_per_pack'])
            
            # Decrement product stock quantity
            cur.execute(
                "UPDATE products SET stock_quantity = stock_quantity - %s WHERE id = %s",
                (qty, prod_id)
            )
            
            # Check if a purchase row already exists for this customer + product + date (upsert logic)
            cur.execute(
                "SELECT id, quantity FROM purchases WHERE customer_id = %s AND product_id = %s AND purchase_date = %s AND bill_id IS NULL",
                (customer_id, prod_id, today)
            )
            existing = cur.fetchone()
            
            if existing:
                # Increment quantity on the existing row and link to bill
                cur.execute(
                    "UPDATE purchases SET quantity = quantity + %s, unit_price = %s, bill_id = %s WHERE id = %s",
                    (qty, unit_price, bill_id, existing['id'])
                )
            else:
                # Insert a new purchase row with the quantity, unit_price, and bill_id
                cur.execute(
                    "INSERT INTO purchases (customer_id, product_id, purchase_date, quantity, unit_price, bill_id) VALUES (%s, %s, %s, %s, %s, %s)",
                    (customer_id, prod_id, today, qty, unit_price, bill_id)
                )
        
        mysql.connection.commit()
        total_items = sum(product_quantities.values())
        return jsonify({
            'success': True,
            'bill_id': bill_id,
            'total_amount': grand_total,
            'message': f'Bill #{bill_id} created — {total_items} item(s), ₹{grand_total:.2f} total.'
        })
    except Exception as e:
        mysql.connection.rollback()
        print(f"[ERROR] Checkout failed: {e}")
        return jsonify({'success': False, 'message': 'Something went wrong. Please try again or contact support.'})
    finally:
        cur.close()

@app.route('/staff/billing-history')
@login_required
@role_required(['staff', 'admin'])
def billing_history():
    """Shows a list of all bills with customer info, date, total, and item count."""
    store_id = request.args.get('store_id', type=int) if session.get('user_role') == 'super_admin' else session.get('store_id')
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    is_global = (session.get('user_role') == 'super_admin' and store_id is None) or (session.get('user_role') == 'admin' and session.get('store_id') is None)
    if is_global:
        cur.execute("""
            SELECT b.id, b.bill_date, b.total_amount,
                   u.name AS customer_name, u.phone AS customer_phone,
                   COUNT(p.id) AS item_count
            FROM bills b
            JOIN users u ON b.customer_id = u.id
            LEFT JOIN purchases p ON p.bill_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
        """)
    else:
        cur.execute("""
            SELECT b.id, b.bill_date, b.total_amount,
                   u.name AS customer_name, u.phone AS customer_phone,
                   COUNT(p.id) AS item_count
            FROM bills b
            JOIN users u ON b.customer_id = u.id
            LEFT JOIN purchases p ON p.bill_id = b.id
            WHERE b.store_id = %s
            GROUP BY b.id
            ORDER BY b.id DESC
        """, (store_id,))
    bills = cur.fetchall()
    cur.close()
    return render_template('billing_history.html', bills=bills)

@app.route('/customer/bills')
@login_required
@role_required(['customer'])
def customer_bills():
    """Renders a list of bills/transactions belonging to the logged-in customer."""
    customer_id = session['user_id']
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT b.id, b.bill_date, b.total_amount,
               s.name AS store_name,
               COUNT(p.id) AS item_count
        FROM bills b
        LEFT JOIN stores s ON b.store_id = s.id
        LEFT JOIN purchases p ON p.bill_id = b.id
        WHERE b.customer_id = %s
        GROUP BY b.id
        ORDER BY b.id DESC
    """, (customer_id,))
    bills = cur.fetchall()
    cur.close()
    return render_template('customer_bills.html', bills=bills)

@app.route('/staff/bill/<int:bill_id>')
@login_required
@role_required(['staff', 'admin', 'customer'])
def view_bill(bill_id):
    """Renders a printable invoice for a given bill."""
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    # Fetch bill header with store details
    cur.execute("""
        SELECT b.id, b.bill_date, b.total_amount, b.customer_id, b.store_id,
               c.name AS customer_name, c.phone AS customer_phone, c.email AS customer_email,
               s.name AS staff_name, st.name AS store_name, st.address AS store_address
        FROM bills b
        JOIN users c ON b.customer_id = c.id
        JOIN users s ON b.staff_id = s.id
        LEFT JOIN stores st ON b.store_id = st.id
        WHERE b.id = %s
    """, (bill_id,))
    bill = cur.fetchone()
    
    if not bill:
        cur.close()
        flash('Bill not found.', 'danger')
        if session.get('user_role') == 'customer':
            return redirect(url_for('customer_bills'))
        return redirect(url_for('billing_history'))
        
    # Authorization check for customer role
    if session.get('user_role') == 'customer':
        if bill['customer_id'] != session.get('user_id'):
            cur.close()
            flash('Access denied. You can only view your own bills.', 'danger')
            return redirect(url_for('customer_bills'))
    # Authorization check for staff/admin role
    else:
        store_id = session.get('store_id')
        is_global = session.get('user_role') == 'super_admin' or (session.get('user_role') == 'admin' and store_id is None)
        if not is_global:
            if bill['store_id'] != store_id:
                cur.close()
                flash('Access denied. This bill belongs to another store.', 'danger')
                return redirect(url_for('billing_history'))
    
    # Fetch line items
    cur.execute("""
        SELECT p.name AS product_name, p.pack_size, p.unit, pur.quantity, pur.unit_price,
               (pur.quantity * pur.unit_price) AS line_total
        FROM purchases pur
        JOIN products p ON pur.product_id = p.id
        WHERE pur.bill_id = %s
        ORDER BY pur.id ASC
    """, (bill_id,))
    items = cur.fetchall()
    cur.close()
    
    return render_template('bill_invoice.html', bill=bill, items=items)

@app.route('/staff/write-off/<int:product_id>', methods=['POST'])
@login_required
@role_required(['staff', 'admin'])
def write_off_product(product_id):
    quantity_str = request.form.get('quantity')
    reason = request.form.get('reason', 'expired')
    
    if not quantity_str:
        flash('Quantity is required for write-off.', 'danger')
        if session.get('user_role') == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('staff_dashboard'))
        
    try:
        quantity = int(quantity_str)
        if quantity <= 0:
            flash('Quantity must be greater than zero.', 'danger')
            if session.get('user_role') == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('staff_dashboard'))
    except (ValueError, TypeError):
        flash('Invalid quantity value.', 'danger')
        if session.get('user_role') == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('staff_dashboard'))
        
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        # Get product and current stock
        store_id = session.get('store_id')
        is_global = session.get('user_role') == 'super_admin' or (session.get('user_role') == 'admin' and store_id is None)
        if is_global:
            cur.execute("SELECT name, stock_quantity FROM products WHERE id = %s", (product_id,))
        else:
            cur.execute("SELECT name, stock_quantity FROM products WHERE id = %s AND store_id = %s", (product_id, store_id))
        product = cur.fetchone()
        
        if not product:
            flash('Product not found.', 'danger')
            mysql.connection.rollback()
            if session.get('user_role') == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('staff_dashboard'))
            
        current_stock = product['stock_quantity']
        if quantity > current_stock:
            flash(f'Cannot write off {quantity} units. Only {current_stock} units available in stock.', 'danger')
            mysql.connection.rollback()
            if session.get('user_role') == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('staff_dashboard'))
            
        # Update product stock_quantity
        if is_global:
            cur.execute("UPDATE products SET stock_quantity = stock_quantity - %s WHERE id = %s", (quantity, product_id))
        else:
            cur.execute("UPDATE products SET stock_quantity = stock_quantity - %s WHERE id = %s AND store_id = %s", (quantity, product_id, store_id))
        
        # Log to wastage_log
        cur.execute(
            "INSERT INTO wastage_log (product_id, quantity, reason, logged_by) VALUES (%s, %s, %s, %s)",
            (product_id, quantity, reason, session['user_id'])
        )
        
        mysql.connection.commit()
        flash(f'Successfully wrote off {quantity} units of "{product["name"]}".', 'success')
        
    except Exception as e:
        mysql.connection.rollback()
        print(f"[ERROR] Product write-off failed: {e}")
        flash('Something went wrong. Please try again or contact support.', 'danger')
    finally:
        cur.close()
        
    if session.get('user_role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('staff_dashboard'))

# ================= SUPER ADMIN FUNCTIONS =================

@app.route('/superadmin/dashboard')
@login_required
@role_required(['super_admin'])
def superadmin_dashboard():
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        # 1. Total statistics
        cur.execute("SELECT COUNT(*) AS total FROM stores")
        total_stores = cur.fetchone()['total']
        
        cur.execute("SELECT COALESCE(SUM(total_amount), 0.00) AS total FROM bills")
        total_revenue = float(cur.fetchone()['total'])
        
        cur.execute("SELECT COUNT(*) AS total FROM users WHERE role = 'customer'")
        total_customers = cur.fetchone()['total']
        
        # 2. Stores list with statistics
        cur.execute("""
            SELECT 
                s.id, 
                s.name, 
                s.address, 
                s.owner_email, 
                s.is_active, 
                s.created_at,
                (SELECT COALESCE(SUM(total_amount), 0.00) FROM bills b WHERE b.store_id = s.id) AS revenue,
                (SELECT COUNT(*) FROM products p WHERE p.store_id = s.id) AS product_count,
                (SELECT COUNT(*) FROM users u WHERE u.store_id = s.id AND u.role IN ('staff', 'admin')) AS staff_count
            FROM stores s
            ORDER BY s.id ASC
        """)
        stores_list = cur.fetchall()
        
        return render_template(
            'superadmin_dashboard.html',
            total_stores=total_stores,
            total_revenue=total_revenue,
            total_customers=total_customers,
            stores_list=stores_list
        )
    except Exception as e:
        print(f"[ERROR] Error loading platform console: {e}")
        flash('Something went wrong. Please try again or contact support.', 'danger')
        return redirect(url_for('login'))
    finally:
        cur.close()

@app.route('/superadmin/toggle-store/<int:store_id>', methods=['POST'])
@login_required
@role_required(['super_admin'])
def toggle_store(store_id):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT is_active, name FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        if not store:
            flash("Store not found.", 'danger')
            return redirect(url_for('superadmin_dashboard'))
            
        new_status = not store['is_active']
        cur.execute("UPDATE stores SET is_active = %s WHERE id = %s", (new_status, store_id))
        mysql.connection.commit()
        
        status_str = "activated" if new_status else "suspended"
        flash(f"Store '{store['name']}' has been successfully {status_str}.", 'success')
    except Exception as e:
        mysql.connection.rollback()
        print(f"[ERROR] Failed to update store status: {e}")
        flash('Something went wrong. Please try again or contact support.', 'danger')
    finally:
        cur.close()
        
    return redirect(url_for('superadmin_dashboard'))

@app.route('/superadmin/create-admin', methods=['POST'])
@login_required
@role_required(['super_admin'])
def superadmin_create_admin():
    name = request.form.get('name')
    email = request.form.get('email')
    phone = request.form.get('phone')
    password = request.form.get('password')
    store_id = request.form.get('store_id')
    
    if not all([name, email, phone, password, store_id]):
        flash('All fields are required to create a store admin.', 'danger')
        return redirect(url_for('superadmin_dashboard'))
        
    store_id = int(store_id)
    hashed_password = generate_password_hash(password)
    
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        # Check if email is already registered
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            flash('Email already registered.', 'danger')
            return redirect(url_for('superadmin_dashboard'))
            
        # Verify store exists
        cur.execute("SELECT name FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        if not store:
            flash('Selected store location does not exist.', 'danger')
            return redirect(url_for('superadmin_dashboard'))
            
        # Insert admin user
        cur.execute(
            "INSERT INTO users (name, phone, email, password, role, store_id, is_active) VALUES (%s, %s, %s, %s, 'admin', %s, TRUE)",
            (name, phone, email, hashed_password, store_id)
        )
        mysql.connection.commit()
        flash(f"Admin '{name}' successfully added to store '{store['name']}'.", 'success')
    except Exception as e:
        mysql.connection.rollback()
        print(f"[ERROR] Failed to create store administrator: {e}")
        flash('Something went wrong. Please try again or contact support.', 'danger')
    finally:
        cur.close()
        
    return redirect(url_for('superadmin_dashboard'))

# ================= ADMIN FUNCTIONS =================

@app.route('/admin/dashboard')
@login_required
@role_required(['admin'])
def admin_dashboard():
    store_id = request.args.get('store_id', type=int) if session.get('user_role') == 'super_admin' else session.get('store_id')
    is_super = (session.get('user_role') == 'super_admin' and store_id is None) or (session.get('user_role') == 'admin' and session.get('store_id') is None)
    
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    # 1. Total statistics
    if is_super:
        cur.execute("SELECT COUNT(*) AS total FROM products")
        total_products = cur.fetchone()['total']
        cur.execute("SELECT COALESCE(SUM(quantity), 0) AS total FROM purchases")
        total_purchases = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) AS total FROM alerts_log")
        total_alerts = cur.fetchone()['total']
        cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills")
        total_revenue = float(cur.fetchone()['total'])
        cur.execute("SELECT COUNT(*) AS total FROM bills")
        total_bills = cur.fetchone()['total']
    else:
        cur.execute("SELECT COUNT(*) AS total FROM products WHERE store_id = %s", (store_id,))
        total_products = cur.fetchone()['total']
        cur.execute("SELECT COALESCE(SUM(pur.quantity), 0) AS total FROM purchases pur JOIN products prod ON pur.product_id = prod.id WHERE prod.store_id = %s", (store_id,))
        total_purchases = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) AS total FROM alerts_log al JOIN products p ON al.product_id = p.id WHERE p.store_id = %s", (store_id,))
        total_alerts = cur.fetchone()['total']
        cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills WHERE store_id = %s", (store_id,))
        total_revenue = float(cur.fetchone()['total'])
        cur.execute("SELECT COUNT(*) AS total FROM bills WHERE store_id = %s", (store_id,))
        total_bills = cur.fetchone()['total']
    
    today = datetime.date.today()
    
    # Today's sales
    if is_super:
        cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills WHERE DATE(bill_date) = %s", (today,))
        today_sales = float(cur.fetchone()['total'])
        
        # This month's sales
        first_of_month = today.replace(day=1)
        cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills WHERE DATE(bill_date) >= %s", (first_of_month,))
        monthly_sales = float(cur.fetchone()['total'])
    else:
        cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills WHERE DATE(bill_date) = %s AND store_id = %s", (today, store_id))
        today_sales = float(cur.fetchone()['total'])
        
        # This month's sales
        first_of_month = today.replace(day=1)
        cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills WHERE DATE(bill_date) >= %s AND store_id = %s", (first_of_month, store_id))
        monthly_sales = float(cur.fetchone()['total'])
        
    # Top selling products (by quantity) - Per Batch View
    if is_super:
        cur.execute("""
            SELECT p.name, p.expiry_date, SUM(pur.quantity) AS total_qty, SUM(pur.quantity * pur.unit_price) AS total_revenue
            FROM purchases pur
            JOIN products p ON pur.product_id = p.id
            GROUP BY pur.product_id
            ORDER BY total_qty DESC
            LIMIT 5
        """)
    else:
        cur.execute("""
            SELECT p.name, p.expiry_date, SUM(pur.quantity) AS total_qty, SUM(pur.quantity * pur.unit_price) AS total_revenue
            FROM purchases pur
            JOIN products p ON pur.product_id = p.id
            WHERE p.store_id = %s
            GROUP BY pur.product_id
            ORDER BY total_qty DESC
            LIMIT 5
        """, (store_id,))
    top_products = cur.fetchall()

    # Top selling products (by quantity) - By Product Name View
    if is_super:
        cur.execute("""
            SELECT p.name, SUM(pur.quantity) AS total_qty, SUM(pur.quantity * pur.unit_price) AS total_revenue
            FROM purchases pur
            JOIN products p ON pur.product_id = p.id
            GROUP BY p.name
            ORDER BY total_qty DESC
            LIMIT 5
        """)
    else:
        cur.execute("""
            SELECT p.name, SUM(pur.quantity) AS total_qty, SUM(pur.quantity * pur.unit_price) AS total_revenue
            FROM purchases pur
            JOIN products p ON pur.product_id = p.id
            WHERE p.store_id = %s
            GROUP BY p.name
            ORDER BY total_qty DESC
            LIMIT 5
        """, (store_id,))
    top_products_by_name = cur.fetchall()
    
    # Daily sales chart data (last 10 days)
    if is_super:
        cur.execute("""
            SELECT DATE(bill_date) AS sale_date, SUM(total_amount) AS daily_total
            FROM bills
            GROUP BY DATE(bill_date)
            ORDER BY sale_date DESC
            LIMIT 10
        """)
    else:
        cur.execute("""
            SELECT DATE(bill_date) AS sale_date, SUM(total_amount) AS daily_total
            FROM bills
            WHERE store_id = %s
            GROUP BY DATE(bill_date)
            ORDER BY sale_date DESC
            LIMIT 10
        """, (store_id,))
    sales_chart_rows = cur.fetchall()
    sales_chart_rows = list(reversed(sales_chart_rows))  # chronological order
    sales_labels = [row['sale_date'].strftime("%Y-%m-%d") for row in sales_chart_rows]
    sales_values = [float(row['daily_total']) for row in sales_chart_rows]
    
    # 2. Near expiry inventory (within next 7 days)
    seven_days_later = today + datetime.timedelta(days=7)
    
    if is_super:
        cur.execute("""
            SELECT p.*, u.name AS registered_by_name,
                   (p.expiry_date - %s) AS days_remaining
            FROM products p
            LEFT JOIN users u ON p.registered_by = u.id
            WHERE p.expiry_date BETWEEN %s AND %s
            ORDER BY p.expiry_date ASC
        """, (today, today, seven_days_later))
    else:
        cur.execute("""
            SELECT p.*, u.name AS registered_by_name,
                   (p.expiry_date - %s) AS days_remaining
            FROM products p
            LEFT JOIN users u ON p.registered_by = u.id
            WHERE p.expiry_date BETWEEN %s AND %s AND p.store_id = %s
            ORDER BY p.expiry_date ASC
        """, (today, today, seven_days_later, store_id))
    near_expiry_products = cur.fetchall()
    
    # 3. Alert Logs
    if is_super:
        cur.execute("""
            SELECT al.*, c.name AS customer_name, c.email AS customer_email, p.name AS product_name, p.expiry_date
            FROM alerts_log al
            JOIN users c ON al.customer_id = c.id
            JOIN products p ON al.product_id = p.id
            ORDER BY al.alert_sent_date DESC, al.id DESC
            LIMIT 50
        """)
    else:
        cur.execute("""
            SELECT al.*, c.name AS customer_name, c.email AS customer_email, p.name AS product_name, p.expiry_date
            FROM alerts_log al
            JOIN users c ON al.customer_id = c.id
            JOIN products p ON al.product_id = p.id
            WHERE p.store_id = %s
            ORDER BY al.alert_sent_date DESC, al.id DESC
            LIMIT 50
        """, (store_id,))
    alert_logs = cur.fetchall()
    
    # 4. Chart.js statistics: Alerts by Day
    if is_super:
        cur.execute("""
            SELECT alert_sent_date, COUNT(*) AS count 
            FROM alerts_log 
            GROUP BY alert_sent_date 
            ORDER BY alert_sent_date ASC 
            LIMIT 10
        """)
    else:
        cur.execute("""
            SELECT al.alert_sent_date, COUNT(*) AS count 
            FROM alerts_log al
            JOIN products p ON al.product_id = p.id
            WHERE p.store_id = %s
            GROUP BY al.alert_sent_date 
            ORDER BY al.alert_sent_date ASC 
            LIMIT 10
        """, (store_id,))
    chart_data_rows = cur.fetchall()
    chart_labels = [row['alert_sent_date'].strftime("%Y-%m-%d") for row in chart_data_rows]
    chart_values = [row['count'] for row in chart_data_rows]
    
    # 5. Low stock alerts (stock_quantity <= 5)
    if is_super:
        cur.execute("""
            SELECT name, SUM(stock_quantity) AS total_stock, unit
            FROM products
            GROUP BY name, unit
            HAVING SUM(stock_quantity) <= 5
            ORDER BY total_stock ASC
        """)
    else:
        cur.execute("""
            SELECT name, SUM(stock_quantity) AS total_stock, unit
            FROM products
            WHERE store_id = %s
            GROUP BY name, unit
            HAVING SUM(stock_quantity) <= 5
            ORDER BY total_stock ASC
        """, (store_id,))
    low_stock_products = cur.fetchall()
    
    # 6. Total wastage cost
    if is_super:
        cur.execute("""
            SELECT COALESCE(SUM(w.quantity * p.price_per_pack), 0) AS total
            FROM wastage_log w
            JOIN products p ON w.product_id = p.id
        """)
    else:
        cur.execute("""
            SELECT COALESCE(SUM(w.quantity * p.price_per_pack), 0) AS total
            FROM wastage_log w
            JOIN products p ON w.product_id = p.id
            WHERE p.store_id = %s
        """, (store_id,))
    total_wastage_cost = float(cur.fetchone()['total'])
    
    # 7. Recent product wastage entries
    if is_super:
        cur.execute("""
            SELECT w.*, p.name AS product_name, p.unit, p.price_per_pack, u.name AS logged_by_name,
                   (w.quantity * p.price_per_pack) AS wastage_cost
            FROM wastage_log w
            JOIN products p ON w.product_id = p.id
            LEFT JOIN users u ON w.logged_by = u.id
            ORDER BY w.logged_at DESC, w.id DESC
            LIMIT 20
        """)
    else:
        cur.execute("""
            SELECT w.*, p.name AS product_name, p.unit, p.price_per_pack, u.name AS logged_by_name,
                   (w.quantity * p.price_per_pack) AS wastage_cost
            FROM wastage_log w
            JOIN products p ON w.product_id = p.id
            LEFT JOIN users u ON w.logged_by = u.id
            WHERE p.store_id = %s
            ORDER BY w.logged_at DESC, w.id DESC
            LIMIT 20
        """, (store_id,))
    recent_wastage = cur.fetchall()
    
    # 8. Staff list for store
    staff_list = []
    if store_id:
        cur.execute("SELECT id, name, email, phone, is_active FROM users WHERE store_id = %s AND role = 'staff' ORDER BY name ASC", (store_id,))
        staff_list = cur.fetchall()
        
    # 9. Auto-cleaned wastage log (reason = 'auto_expired')
    if is_super:
        cur.execute("""
            SELECT w.*, p.name AS product_name, p.unit, p.price_per_pack, p.expiry_date AS product_expiry_date,
                   (w.quantity * p.price_per_pack) AS wastage_cost
            FROM wastage_log w
            JOIN products p ON w.product_id = p.id
            WHERE w.reason = 'auto_expired'
            ORDER BY w.logged_at DESC, w.id DESC
            LIMIT 50
        """)
    else:
        cur.execute("""
            SELECT w.*, p.name AS product_name, p.unit, p.price_per_pack, p.expiry_date AS product_expiry_date,
                   (w.quantity * p.price_per_pack) AS wastage_cost
            FROM wastage_log w
            JOIN products p ON w.product_id = p.id
            WHERE w.reason = 'auto_expired' AND p.store_id = %s
            ORDER BY w.logged_at DESC, w.id DESC
            LIMIT 50
        """, (store_id,))
    auto_cleaned_wastage = cur.fetchall()
    
    cur.close()
    
    return render_template(
        'admin_dashboard.html',
        total_products=total_products,
        total_purchases=total_purchases,
        total_alerts=total_alerts,
        total_revenue=total_revenue,
        total_bills=total_bills,
        today_sales=today_sales,
        monthly_sales=monthly_sales,
        top_products=top_products,
        top_products_by_name=top_products_by_name,
        sales_labels=sales_labels,
        sales_values=sales_values,
        near_expiry_products=near_expiry_products,
        alert_logs=alert_logs,
        chart_labels=chart_labels,
        chart_values=chart_values,
        low_stock_products=low_stock_products,
        total_wastage_cost=total_wastage_cost,
        recent_wastage=recent_wastage,
        staff_list=staff_list,
        auto_cleaned_wastage=auto_cleaned_wastage
    )

@app.route('/admin/trigger-scheduler', methods=['GET', 'POST'])
@csrf.exempt
def admin_trigger_scheduler():
    """Trigger endpoint for the daily alerts check. Supports both manual admin session and bearer token cron."""
    is_authenticated = False
    is_cron = False
    
    # Check Bearer Token Auth
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        cron_secret = os.environ.get('CRON_SECRET')
        if cron_secret and token == cron_secret:
            is_authenticated = True
            is_cron = True
            
    # Check Session Auth
    if not is_authenticated and session.get('user_id') and session.get('user_role') in ['admin', 'super_admin']:
        is_authenticated = True

    if not is_authenticated:
        return jsonify({'success': False, 'message': 'Unauthorized.'}), 401
        
    alerts_sent_count = run_expiry_alerts_check()
    
    if is_cron:
        return jsonify({'success': True, 'alerts_sent': alerts_sent_count})
        
    flash(f"Scheduler run completed. Generated {alerts_sent_count} alerts.", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/trigger-cleanup', methods=['GET', 'POST'])
@csrf.exempt
def admin_trigger_cleanup():
    """Trigger endpoint for the auto-expiry stock cleanup. Supports both manual admin session and bearer token cron."""
    is_authenticated = False
    is_cron = False
    
    # Check Bearer Token Auth
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        cron_secret = os.environ.get('CRON_SECRET')
        if cron_secret and token == cron_secret:
            is_authenticated = True
            is_cron = True
            
    # Check Session Auth
    if not is_authenticated and session.get('user_id') and session.get('user_role') in ['admin', 'super_admin']:
        is_authenticated = True

    if not is_authenticated:
        return jsonify({'success': False, 'message': 'Unauthorized.'}), 401
        
    cleaned_count = run_auto_expiry_cleanup()
    
    if is_cron:
        return jsonify({'success': True, 'cleaned_count': cleaned_count})
        
    flash(f"Auto-cleanup run completed. Cleared stock for {cleaned_count} expired products.", "success")
    return redirect(url_for('admin_dashboard'))

# ================= CUSTOMER FUNCTIONS =================

@app.route('/customer/dashboard')
@login_required
@role_required(['customer'])
def customer_dashboard():
    customer_id = session['user_id']
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    # 1. Personal expiry timeline (purchased products sorted by expiry_date)
    today = datetime.date.today()
    cur.execute("""
        SELECT pur.purchase_date, p.name AS product_name, p.expiry_date,
               p.pack_size, p.unit,
               pur.quantity,
               (p.expiry_date - %s) AS days_remaining
        FROM purchases pur
        JOIN products p ON pur.product_id = p.id
        WHERE pur.customer_id = %s
        ORDER BY p.expiry_date ASC
    """, (today, customer_id))
    timeline = cur.fetchall()
    
    # 2. Customer Notification History
    cur.execute("""
        SELECT al.*, p.name AS product_name, p.expiry_date
        FROM alerts_log al
        JOIN products p ON al.product_id = p.id
        WHERE al.customer_id = %s
        ORDER BY al.alert_sent_date DESC, al.id DESC
    """, (customer_id,))
    notifications = cur.fetchall()
    
    cur.close()
    return render_template('customer_dashboard.html', timeline=timeline, notifications=notifications)


# ================= DAILY SCHEDULER & ALERT FUNCTION =================

def run_expiry_alerts_check():
    """
    Main job that checks for purchased products that are 7, 3, or 1 days away from expiry
    and emails the customer and admin, recording the result in alerts_log.
    """
    print("[SCHEDULER] Running daily retail expiry alerts check...")
    
    # Since we are using Flask-MySQLdb which requires request contexts,
    # we need to open a direct MySQLdb connection inside the scheduler thread.
    db_url = app.config['DATABASE_URL']
    if not db_url:
        db_url = "postgresql://postgres:postgres@localhost:5432/expiry_system"
    db = psycopg2.connect(db_url)
    cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    alerts_count = 0
    today = datetime.date.today()
    
    # Define trigger levels: 7 days, 3 days, 1 day
    alert_intervals = [7, 3, 1]
    
    try:
        for interval in alert_intervals:
            target_date = today + datetime.timedelta(days=interval)
            
            # Find purchases where the product's expiry_date is exactly target_date
            cursor.execute("""
                SELECT pur.id AS purchase_id, pur.customer_id, pur.product_id,
                       c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone,
                       p.name AS product_name, p.expiry_date
                FROM purchases pur
                JOIN users c ON pur.customer_id = c.id
                JOIN products p ON pur.product_id = p.id
                WHERE p.expiry_date = %s
            """, (target_date,))
            
            due_purchases = cursor.fetchall()
            
            for p in due_purchases:
                customer_id = p['customer_id']
                product_id = p['product_id']
                cust_email = p['customer_email']
                cust_name = p['customer_name']
                prod_name = p['product_name']
                cust_phone = p['customer_phone']
                
                # Check and send Email Alert if email exists
                if cust_email:
                    # Check if this email alert has already been logged to avoid duplicates
                    cursor.execute("""
                        SELECT id FROM alerts_log 
                        WHERE customer_id = %s AND product_id = %s AND days_before_expiry = %s AND method = 'email'
                    """, (customer_id, product_id, interval))
                    
                    existing_email_alert = cursor.fetchone()
                    
                    if not existing_email_alert:
                        # Construct email alert text
                        subject = f"Alert: Your product '{prod_name}' is expiring in {interval} day(s)!"
                        body = f"""
                        <h2>Retail Expiry Tracker &amp; Consumer Alert System</h2>
                        <p>Dear {cust_name},</p>
                        <p>This is an automated alert to notify you that the product <strong>{prod_name}</strong> you purchased is expiring soon.</p>
                        <p><strong>Product Details:</strong></p>
                        <ul>
                            <li>Product Name: {prod_name}</li>
                            <li>Expiry Date: {p['expiry_date'].strftime('%Y-%m-%d')}</li>
                            <li>Days remaining: {interval} day(s)</li>
                        </ul>
                        <p>Please consume or dispose of the product accordingly.</p>
                        <p>Thank you for shopping with us!</p>
                        """
                        
                        print(f"[SCHEDULER] Triggered expiry EMAIL alert ({interval} days) for {cust_name} - {prod_name}")
                        
                        # Send alert email
                        send_email_alert(subject, body, cust_email)
                        
                        # Insert record into alerts_log
                        cursor.execute("""
                            INSERT INTO alerts_log (customer_id, product_id, alert_sent_date, days_before_expiry, recipient, method)
                            VALUES (%s, %s, %s, %s, 'both', 'email')
                        """, (customer_id, product_id, today, interval))
                        
                        alerts_count += 1
                
                # Check and send SMS Alert if phone exists
                if cust_phone:
                    # Check if this SMS alert has already been logged to avoid duplicates
                    cursor.execute("""
                        SELECT id FROM alerts_log 
                        WHERE customer_id = %s AND product_id = %s AND days_before_expiry = %s AND method = 'sms'
                    """, (customer_id, product_id, interval))
                    
                    existing_sms_alert = cursor.fetchone()
                    
                    if not existing_sms_alert:
                        # Construct SMS text
                        sms_message = f"Hi {cust_name}, your product {prod_name} expires in {interval} day(s). Please consume it soon."
                        
                        print(f"[SCHEDULER] Triggered expiry SMS alert ({interval} days) for {cust_name} - {prod_name}")
                        
                        # Send alert SMS
                        send_sms_alert(cust_phone, sms_message)
                        
                        # Insert record into alerts_log
                        cursor.execute("""
                            INSERT INTO alerts_log (customer_id, product_id, alert_sent_date, days_before_expiry, recipient, method)
                            VALUES (%s, %s, %s, %s, 'both', 'sms')
                        """, (customer_id, product_id, today, interval))
                        
                        alerts_count += 1
                    
        db.commit()
    except Exception as ex:
        db.rollback()
        print(f"[SCHEDULER ERROR] Failed to complete daily alerts check: {ex}")
    finally:
        cursor.close()
        db.close()
        
    print(f"[SCHEDULER] Expiry alerts check finished. Total alerts sent/logged: {alerts_count}")
    return alerts_count


def run_auto_expiry_cleanup():
    """
    Main job that clears products that have been expired for more than 3 days.
    Sets stock_quantity to 0 and records it in wastage_log.
    """
    print("[SCHEDULER] Running daily automated expiry cleanup...")
    
    db_url = app.config['DATABASE_URL']
    if not db_url:
        db_url = "postgresql://postgres:postgres@localhost:5432/expiry_system"
    db = psycopg2.connect(db_url)
    cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    cleaned_count = 0
    today = datetime.date.today()
    
    try:
        # Query products expired for more than 3 days with positive stock_quantity
        cursor.execute("""
            SELECT id, name, stock_quantity, expiry_date 
            FROM products 
            WHERE expiry_date < (CURRENT_DATE - 3) AND stock_quantity > 0
        """)
        expired_products = cursor.fetchall()
        
        for prod in expired_products:
            prod_id = prod['id']
            name = prod['name']
            qty = prod['stock_quantity']
            expiry = prod['expiry_date']
            days_overdue = (today - expiry).days
            
            # Log in wastage_log
            cursor.execute("""
                INSERT INTO wastage_log (product_id, quantity, reason, logged_by) 
                VALUES (%s, %s, 'auto_expired', NULL)
            """, (prod_id, qty))
            
            # Update product quantity to 0
            cursor.execute("""
                UPDATE products 
                SET stock_quantity = 0 
                WHERE id = %s
            """, (prod_id,))
            
            print(f"[AUTO-CLEANUP] Cleared {qty} units of {name} (expired {days_overdue} days ago)")
            cleaned_count += 1
            
        db.commit()
    except Exception as ex:
        db.rollback()
        print(f"[SCHEDULER ERROR] Failed to complete auto-cleanup: {ex}")
        raise ex
    finally:
        cursor.close()
        db.close()
        
    print(f"[SCHEDULER] Auto-cleanup finished. Total products cleaned up: {cleaned_count}")
    return cleaned_count


# Setup background scheduler
# Note: BackgroundScheduler will not run reliably on serverless environments like Vercel.
# In production on Vercel, the scheduler initialization is bypassed, and daily cron
# triggers are executed externally via the /admin/trigger-* endpoints.
# The scheduler is kept active below for local development and non-production environments.
scheduler = BackgroundScheduler()
if (not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true') and os.environ.get('FLASK_ENV') != 'production':
    lock_file_path = os.path.join(base_dir, 'scheduler.lock')
    should_start = True
    try:
        import fcntl
        global _scheduler_lock_file
        _scheduler_lock_file = open(lock_file_path, 'w')
        fcntl.lockf(_scheduler_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        # Windows or non-UNIX environment, bypass file locking
        print("[SCHEDULER] fcntl module not available. Proceeding without file lock (dev environment).")
    except (BlockingIOError, PermissionError):
        # Lock acquired by another process
        print("[SCHEDULER] Another process holds the scheduler lock. Skipping scheduler startup in this process.")
        should_start = False
    except OSError as e:
        import errno
        if hasattr(e, 'errno') and e.errno in (errno.EAGAIN, errno.EACCES):
            print("[SCHEDULER] Another process holds the scheduler lock. Skipping scheduler startup in this process.")
            should_start = False
        else:
            print(f"[SCHEDULER WARNING] Failed to acquire file lock: {e}. Starting scheduler anyway.")
    except Exception as e:
        print(f"[SCHEDULER WARNING] Failed to acquire file lock: {e}. Starting scheduler anyway.")
    
    if should_start:
        scheduler.add_job(func=run_expiry_alerts_check, trigger="cron", hour=0, minute=0)
        scheduler.add_job(func=run_auto_expiry_cleanup, trigger="cron", hour=1, minute=0)
        scheduler.start()
        print("[SCHEDULER] APScheduler started successfully with daily alerts and auto-cleanup jobs.")

if __name__ == '__main__':
    app.run(debug=True)