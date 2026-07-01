"""
seed_neon_db.py
---------------
Creates all tables in Neon PostgreSQL (from expiry_system_pg.sql) and seeds
initial users and a default store. Safe to run multiple times — uses
INSERT ... ON CONFLICT DO NOTHING to avoid duplicates.

Run:
    python seed_neon_db.py
"""
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

    db_url = os.environ.get('DATABASE_URL_DIRECT') or os.environ.get('DATABASE_URL')
    if not db_url:
        print("[FAILURE] DATABASE_URL is not set in .env")
        sys.exit(1)

    print(f"Connecting to Neon PostgreSQL...")
    conn = None
    try:
        conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
        cur = conn.cursor()

        # ── 1. Create schema ──────────────────────────────────────────────────
        print("Creating tables (IF NOT EXISTS)...")
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

        # ── 2. Seed default store ─────────────────────────────────────────────
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
            # Already exists — fetch the first store
            cur.execute("SELECT id FROM stores ORDER BY id ASC LIMIT 1")
            store_id = cur.fetchone()['id']
            print(f" - Store already exists, using id={store_id}")
        conn.commit()

        # ── 3. Seed users ─────────────────────────────────────────────────────
        users = [
            # (name, phone, email, plaintext_password, role, store_id)
            ("Super Admin",   "0000000000", "superadmin@retail.com", "superadmin123", "super_admin", None),
            ("Store Admin",   "1111111111", "admin@retail.com",      "admin123",      "admin",       store_id),
            ("Store Staff",   "2222222222", "staff@retail.com",      "staff123",      "staff",       store_id),
            ("Jane Customer", "3333333333", "customer@retail.com",   "customer123",   "customer",    None),
            ("John Doe",      "9876543210", "john@gmail.com",        "john123",       "customer",    None),
        ]

        print("Seeding users...")
        for name, phone, email, plaintext_pw, role, sid in users:
            hashed_pw = generate_password_hash(plaintext_pw)
            cur.execute("""
                INSERT INTO users (name, phone, email, password, role, store_id, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (email) DO NOTHING
            """, (name, phone, email, hashed_pw, role, sid))
            print(f"   - {role:12s} | {email:30s} | password: {plaintext_pw}")

        conn.commit()
        print("\n[SUCCESS] Neon database seeded successfully!")
        print("\nLogin credentials:")
        print("  superadmin@retail.com  / superadmin123")
        print("  admin@retail.com       / admin123")
        print("  staff@retail.com       / staff123")
        print("  customer@retail.com    / customer123")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"\n[FAILURE] Seeding failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    main()
