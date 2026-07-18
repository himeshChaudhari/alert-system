import os
import sys
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash

def main():
    print("Loading .env environment variables...")
    base_dir = os.path.abspath(os.path.dirname(__file__))
    load_dotenv(os.path.join(base_dir, '.env'))

    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        print("[FAILURE] DATABASE_URL is not set in .env")
        sys.exit(1)

    print("Connecting to PostgreSQL...")
    conn = None
    try:
        conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
        cur = conn.cursor()

        # Create schema
        print("Creating tables if they do not exist...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                id          SERIAL PRIMARY KEY,
                name        VARCHAR(100) NOT NULL,
                address     VARCHAR(255) DEFAULT NULL,
                owner_email VARCHAR(100) NOT NULL,
                is_active   BOOLEAN NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id        SERIAL PRIMARY KEY,
                name      VARCHAR(100),
                phone     VARCHAR(15),
                email     VARCHAR(100) UNIQUE,
                password  VARCHAR(200),
                role      VARCHAR(20) NOT NULL DEFAULT 'customer'
                              CHECK (role IN ('customer','staff','admin','super_admin')),
                store_id  INT DEFAULT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                FOREIGN KEY (store_id) REFERENCES stores(id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id             SERIAL PRIMARY KEY,
                name           VARCHAR(100),
                expiry_date    DATE,
                price_per_pack DECIMAL(10,2) NOT NULL DEFAULT 0.00,
                stock_quantity INT NOT NULL DEFAULT 0,
                pack_size      DECIMAL(10,2) NOT NULL DEFAULT 1.00,
                unit           VARCHAR(20) NOT NULL DEFAULT 'piece',
                qr_code_data   VARCHAR(200),
                registered_by  INT,
                store_id       INT NOT NULL,
                FOREIGN KEY (registered_by) REFERENCES users(id),
                FOREIGN KEY (store_id) REFERENCES stores(id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bills (
                id           SERIAL PRIMARY KEY,
                customer_id  INT NOT NULL,
                staff_id     INT NOT NULL,
                total_amount DECIMAL(10,2) NOT NULL DEFAULT 0.00,
                bill_date    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                store_id     INT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES users(id),
                FOREIGN KEY (staff_id)    REFERENCES users(id),
                FOREIGN KEY (store_id)    REFERENCES stores(id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id            SERIAL PRIMARY KEY,
                customer_id   INT,
                product_id    INT,
                purchase_date DATE,
                quantity      INT DEFAULT 1,
                unit_price    DECIMAL(10,2) NOT NULL DEFAULT 0.00,
                bill_id       INT NULL,
                FOREIGN KEY (customer_id) REFERENCES users(id),
                FOREIGN KEY (product_id)  REFERENCES products(id),
                FOREIGN KEY (bill_id)     REFERENCES bills(id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS alerts_log (
                id                 SERIAL PRIMARY KEY,
                customer_id        INT,
                product_id         INT,
                alert_sent_date    DATE,
                days_before_expiry INT,
                recipient          VARCHAR(10) NOT NULL DEFAULT 'both'
                                       CHECK (recipient IN ('customer','admin','both')),
                method             VARCHAR(10) NOT NULL DEFAULT 'email'
                                       CHECK (method IN ('email','sms')),
                FOREIGN KEY (customer_id) REFERENCES users(id),
                FOREIGN KEY (product_id)  REFERENCES products(id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wastage_log (
                id         SERIAL PRIMARY KEY,
                product_id INT NOT NULL,
                quantity   INT NOT NULL,
                reason     VARCHAR(100) DEFAULT 'expired',
                logged_by  INT NULL,
                logged_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id),
                FOREIGN KEY (logged_by)  REFERENCES users(id)
            )
        """)
        conn.commit()
        print("Tables created successfully.")

        # Seed default store
        print("Seeding default store...")
        cur.execute("""
            INSERT INTO stores (name, address, owner_email, is_active)
            VALUES ('Main Branch Store', 'Main Street, City Centre', 'owner@retail.com', TRUE)
            ON CONFLICT DO NOTHING
            RETURNING id
        """)
        row = cur.fetchone()
        if row:
            store_id = row['id']
            print(f" - Store inserted with id={store_id}")
        else:
            cur.execute("SELECT id FROM stores ORDER BY id ASC LIMIT 1")
            store_id = cur.fetchone()['id']
            print(f" - Store already exists, using id={store_id}")
        conn.commit()

        # 1. Define required environment variables for seeding
        required_env_vars = [
            "SUPERADMIN_NAME", "SUPERADMIN_PHONE", "SUPERADMIN_EMAIL", "SUPERADMIN_PASSWORD",
            "STORE_ADMIN_NAME", "STORE_ADMIN_PHONE", "STORE_ADMIN_EMAIL", "STORE_ADMIN_PASSWORD",
            "STAFF_NAME", "STAFF_PHONE", "STAFF_EMAIL", "STAFF_PASSWORD",
            "CUSTOMER_NAME", "CUSTOMER_PHONE", "CUSTOMER_EMAIL", "CUSTOMER_PASSWORD",
            "CUSTOMER2_NAME", "CUSTOMER2_PHONE", "CUSTOMER2_EMAIL", "CUSTOMER2_PASSWORD"
        ]
        
        # 2. Check for missing variables and notify the user with a descriptive list
        missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
        if missing_vars:
            print("\n[FAILURE] Missing the following required environment variables for seeding:")
            for var in missing_vars:
                print(f" - {var}")
            sys.exit(1)

        # 3. Construct default users list from environment variables
        users = [
            (os.environ.get("SUPERADMIN_NAME"),    os.environ.get("SUPERADMIN_PHONE"),    os.environ.get("SUPERADMIN_EMAIL"),    os.environ.get("SUPERADMIN_PASSWORD"),    "super_admin", None),
            (os.environ.get("STORE_ADMIN_NAME"),   os.environ.get("STORE_ADMIN_PHONE"),   os.environ.get("STORE_ADMIN_EMAIL"),   os.environ.get("STORE_ADMIN_PASSWORD"),   "admin",       store_id),
            (os.environ.get("STAFF_NAME"),         os.environ.get("STAFF_PHONE"),         os.environ.get("STAFF_EMAIL"),         os.environ.get("STAFF_PASSWORD"),         "staff",       store_id),
            (os.environ.get("CUSTOMER_NAME"),      os.environ.get("CUSTOMER_PHONE"),      os.environ.get("CUSTOMER_EMAIL"),      os.environ.get("CUSTOMER_PASSWORD"),      "customer",    None),
            (os.environ.get("CUSTOMER2_NAME"),     os.environ.get("CUSTOMER2_PHONE"),     os.environ.get("CUSTOMER2_EMAIL"),     os.environ.get("CUSTOMER2_PASSWORD"),     "customer",    None),
        ]

        # 4. Hash passwords and insert into PostgreSQL
        print("Seeding users...")
        for name, phone, email, plaintext_pw, role, sid in users:
            hashed_pw = generate_password_hash(plaintext_pw)
            cur.execute("""
                INSERT INTO users (name, phone, email, password, role, store_id, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (email) DO NOTHING
            """, (name, phone, email, hashed_pw, role, sid))
            # Safe logging: only print user roles and emails, never print plaintext/hashed passwords
            print(f"   - {role:12s} | {email:30s}")

        conn.commit()
        print("\n[SUCCESS] Supabase database seeded successfully!")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"\n[FAILURE] Seeding failed: {e}")
        sys.exit(1)
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    main()
