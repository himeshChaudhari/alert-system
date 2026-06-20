import sys
import pymysql
import pymysql.cursors

pymysql.install_as_MySQLdb()
sys.modules['MySQLdb.cursors'] = pymysql.cursors

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file, g
from collections import Counter
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
base_dir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(base_dir, '.env'))

app = Flask(__name__)
app.secret_key = os.environ.get('secret_key', 'fallback_secret_key')

# Database Configuration
app.config['MYSQL_HOST'] = os.environ.get('MYSQL_HOST', 'localhost')
app.config['MYSQL_USER'] = os.environ.get('MYSQL_USER', 'root')
app.config['MYSQL_PASSWORD'] = os.environ.get('MYSQL_PASSWORD')
app.config['MYSQL_DB'] = os.environ.get('MYSQL_DB', 'expiry_system')
app.config['TEXTBEE_API_KEY'] = os.environ.get('TEXTBEE_API_KEY')
app.config['TEXTBEE_DEVICE_ID'] = os.environ.get('TEXTBEE_DEVICE_ID')

class MySQL:
    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        @app.teardown_appcontext
        def teardown_db(exception):
            db = g.pop('mysql_db', None)
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    @property
    def connection(self):
        if 'mysql_db' not in g:
            from flask import current_app
            g.mysql_db = pymysql.connect(
                host=current_app.config['MYSQL_HOST'],
                user=current_app.config['MYSQL_USER'],
                password=current_app.config['MYSQL_PASSWORD'],
                database=current_app.config['MYSQL_DB']
            )
        return g.mysql_db

mysql = MySQL(app)

# Email Settings (Modify these for real SMTP servers like Gmail, Mailtrap, etc.)
SMTP_HOST = os.environ.get('SMTP_HOST')
SMTP_PORT = int(os.environ.get('SMTP_PORT'))
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
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session or session['user_role'] not in allowed_roles:
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

@app.route('/test-sms')
def test_sms():
    result = send_sms_alert(
        "8975328505",
        "Hello Himesh! SMS automation is working."
    )

    return "SMS Sent!" if result else "SMS Failed!"

@app.route('/dashboard')
@login_required
def dashboard_router():
    """Redirects the user to the correct dashboard based on their role."""
    role = session.get('user_role')
    if role == 'admin':
        return redirect(url_for('admin_dashboard'))
    elif role == 'staff':
        return redirect(url_for('staff_dashboard'))
    elif role == 'customer':
        return redirect(url_for('customer_dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
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
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['user_email'] = user['email']
            session['user_role'] = user['role']
            flash(f"Welcome back, {user['name']}!", 'success')
            return redirect(url_for('dashboard_router'))
        else:
            flash('Invalid email or password.', 'danger')
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('dashboard_router'))
        
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        password = request.form.get('password')
        role = request.form.get('role', 'customer') # Defaults to customer
        
        # Admin cannot be registered normally, must be customer or staff
        if role == 'admin':
            role = 'customer'
            
        hashed_password = generate_password_hash(password)
        
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            # 1. Check if user with this phone number already exists
            cur.execute("SELECT * FROM users WHERE phone = %s", (phone,))
            existing_user = cur.fetchone()
            
            if existing_user:
                # If they exist and have NO email, they were auto-created at billing counter
                if not existing_user['email']:
                    # Update email, password, name, and role
                    updated_role = role if existing_user['role'] != 'admin' else 'admin'
                    cur.execute(
                        "UPDATE users SET name = %s, email = %s, password = %s, role = %s WHERE phone = %s",
                        (name, email, hashed_password, updated_role, phone)
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
                "INSERT INTO users (name, phone, email, password, role) VALUES (%s, %s, %s, %s, %s)",
                (name, phone, email, hashed_password, role)
            )
            mysql.connection.commit()
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            mysql.connection.rollback()
            flash(f'Registration failed: {e}', 'danger')
        finally:
            cur.close()
            
    return render_template('register.html')

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
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM products ORDER BY id DESC")
    products = cur.fetchall()
    cur.close()
    return render_template('staff_dashboard.html', products=products)

@app.route('/staff/product-price/<int:product_id>')
@login_required
@role_required(['staff', 'admin'])
def get_product_price(product_id):
    """API endpoint: returns product name & price for the billing cart."""
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT id, name, price_per_pack, stock_quantity, pack_size, unit FROM products WHERE id = %s", (product_id,))
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
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT qr_code_data FROM products WHERE id = %s", (product_id,))
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
    
    cur = mysql.connection.cursor()
    try:
        # 1. Insert product into DB with price (temporarily without QR code data)
        cur.execute(
            "INSERT INTO products (name, expiry_date, price_per_pack, stock_quantity, pack_size, unit, qr_code_data, registered_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (name, expiry_date, price_per_pack, stock_quantity, pack_size, unit, "", staff_id)
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
        flash(f'Error registering product: {e}', 'danger')
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
        return jsonify({'success': False, 'message': f'Failed to create customer: {e}'})
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
        
        # Fetch prices and stock for all products in this transaction
        prod_id_list = list(product_quantities.keys())
        format_strings = ','.join(['%s'] * len(prod_id_list))
        cur.execute(f"SELECT id, name, price_per_pack, stock_quantity FROM products WHERE id IN ({format_strings})", tuple(prod_id_list))
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
            "INSERT INTO bills (customer_id, staff_id, total_amount, bill_date) VALUES (%s, %s, %s, %s)",
            (customer_id, staff_id, grand_total, now)
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
        return jsonify({'success': False, 'message': f'Checkout failed: {e}'})
    finally:
        cur.close()

@app.route('/staff/billing-history')
@login_required
@role_required(['staff', 'admin'])
def billing_history():
    """Shows a list of all bills with customer info, date, total, and item count."""
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
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
    bills = cur.fetchall()
    cur.close()
    return render_template('billing_history.html', bills=bills)

@app.route('/staff/bill/<int:bill_id>')
@login_required
@role_required(['staff', 'admin'])
def view_bill(bill_id):
    """Renders a printable invoice for a given bill."""
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    # Fetch bill header
    cur.execute("""
        SELECT b.id, b.bill_date, b.total_amount,
               c.name AS customer_name, c.phone AS customer_phone, c.email AS customer_email,
               s.name AS staff_name
        FROM bills b
        JOIN users c ON b.customer_id = c.id
        JOIN users s ON b.staff_id = s.id
        WHERE b.id = %s
    """, (bill_id,))
    bill = cur.fetchone()
    
    if not bill:
        flash('Bill not found.', 'danger')
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

# ================= ADMIN FUNCTIONS =================

@app.route('/admin/dashboard')
@login_required
@role_required(['admin'])
def admin_dashboard():
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    # 1. Total statistics
    cur.execute("SELECT COUNT(*) AS total FROM products")
    total_products = cur.fetchone()['total']
    
    cur.execute("SELECT COALESCE(SUM(quantity), 0) AS total FROM purchases")
    total_purchases = cur.fetchone()['total']
    
    cur.execute("SELECT COUNT(*) AS total FROM alerts_log")
    total_alerts = cur.fetchone()['total']
    
    # Revenue statistics
    cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills")
    total_revenue = float(cur.fetchone()['total'])
    
    cur.execute("SELECT COUNT(*) AS total FROM bills")
    total_bills = cur.fetchone()['total']
    
    today = datetime.date.today()
    
    # Today's sales
    cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills WHERE DATE(bill_date) = %s", (today,))
    today_sales = float(cur.fetchone()['total'])
    
    # This month's sales
    first_of_month = today.replace(day=1)
    cur.execute("SELECT COALESCE(SUM(total_amount), 0) AS total FROM bills WHERE DATE(bill_date) >= %s", (first_of_month,))
    monthly_sales = float(cur.fetchone()['total'])
    
    # Top selling products (by quantity)
    cur.execute("""
        SELECT p.name, SUM(pur.quantity) AS total_qty, SUM(pur.quantity * pur.unit_price) AS total_revenue
        FROM purchases pur
        JOIN products p ON pur.product_id = p.id
        GROUP BY pur.product_id
        ORDER BY total_qty DESC
        LIMIT 5
    """)
    top_products = cur.fetchall()
    
    # Daily sales chart data (last 10 days)
    cur.execute("""
        SELECT DATE(bill_date) AS sale_date, SUM(total_amount) AS daily_total
        FROM bills
        GROUP BY DATE(bill_date)
        ORDER BY sale_date DESC
        LIMIT 10
    """)
    sales_chart_rows = cur.fetchall()
    sales_chart_rows = list(reversed(sales_chart_rows))  # chronological order
    sales_labels = [row['sale_date'].strftime("%Y-%m-%d") for row in sales_chart_rows]
    sales_values = [float(row['daily_total']) for row in sales_chart_rows]
    
    # 2. Near expiry inventory (within next 7 days)
    seven_days_later = today + datetime.timedelta(days=7)
    
    cur.execute("""
        SELECT p.*, u.name AS registered_by_name,
               DATEDIFF(p.expiry_date, %s) AS days_remaining
        FROM products p
        LEFT JOIN users u ON p.registered_by = u.id
        WHERE p.expiry_date BETWEEN %s AND %s
        ORDER BY p.expiry_date ASC
    """, (today, today, seven_days_later))
    near_expiry_products = cur.fetchall()
    
    # 3. Alert Logs
    cur.execute("""
        SELECT al.*, c.name AS customer_name, c.email AS customer_email, p.name AS product_name, p.expiry_date
        FROM alerts_log al
        JOIN users c ON al.customer_id = c.id
        JOIN products p ON al.product_id = p.id
        ORDER BY al.alert_sent_date DESC, al.id DESC
        LIMIT 50
    """)
    alert_logs = cur.fetchall()
    
    # 4. Chart.js statistics: Alerts by Day
    cur.execute("""
        SELECT alert_sent_date, COUNT(*) AS count 
        FROM alerts_log 
        GROUP BY alert_sent_date 
        ORDER BY alert_sent_date ASC 
        LIMIT 10
    """)
    chart_data_rows = cur.fetchall()
    chart_labels = [row['alert_sent_date'].strftime("%Y-%m-%d") for row in chart_data_rows]
    chart_values = [row['count'] for row in chart_data_rows]
    
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
        sales_labels=sales_labels,
        sales_values=sales_values,
        near_expiry_products=near_expiry_products,
        alert_logs=alert_logs,
        chart_labels=chart_labels,
        chart_values=chart_values
    )

@app.route('/admin/trigger-scheduler', methods=['POST'])
@login_required
@role_required(['admin'])
def admin_trigger_scheduler():
    """Manual scheduler trigger endpoint for testing and verification purposes."""
    alerts_sent_count = run_expiry_alerts_check()
    flash(f"Scheduler run completed. Generated {alerts_sent_count} alerts.", "success")
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
               DATEDIFF(p.expiry_date, %s) AS days_remaining
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
    db = MySQLdb.connect(
        host=app.config['MYSQL_HOST'],
        user=app.config['MYSQL_USER'],
        passwd=app.config['MYSQL_PASSWORD'],
        db=app.config['MYSQL_DB']
    )
    cursor = db.cursor(MySQLdb.cursors.DictCursor)
    
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

# Setup background scheduler
scheduler = BackgroundScheduler()
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
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
        scheduler.add_job(func=run_expiry_alerts_check, trigger="interval", days=1)
        scheduler.start()
        print("[SCHEDULER] APScheduler started successfully.")

if __name__ == '__main__':
    app.run(debug=True)