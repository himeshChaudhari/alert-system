-- ============================================================
-- Neon PostgreSQL Schema — Retail Expiry Alert System
-- Converted from MySQL (expiry_system.sql)
--
-- Key differences from MySQL original:
--   AUTO_INCREMENT  → SERIAL
--   ENUM(...)       → VARCHAR(20) CHECK (...)
--   DATETIME        → TIMESTAMP
--   TINYINT(1)      → BOOLEAN
--   CREATE DATABASE / USE dropped (Neon manages the DB)
--   ON UPDATE CURRENT_TIMESTAMP removed (not supported in PG)
--
-- Run this file against your Neon database once via:
--   psql "<DATABASE_URL>" -f expiry_system_pg.sql
-- ============================================================

-- stores
CREATE TABLE IF NOT EXISTS stores (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    address     VARCHAR(255) DEFAULT NULL,
    owner_email VARCHAR(100) NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- users
-- role ENUM → VARCHAR + CHECK constraint (easier to alter later than a PG TYPE)
CREATE TABLE IF NOT EXISTS users (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(100),
    phone      VARCHAR(15),
    email      VARCHAR(100) UNIQUE,
    password   VARCHAR(200),
    role       VARCHAR(20) NOT NULL DEFAULT 'customer'
                   CHECK (role IN ('customer', 'staff', 'admin', 'super_admin')),
    store_id   INT DEFAULT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    FOREIGN KEY (store_id) REFERENCES stores(id)
);

-- products
CREATE TABLE IF NOT EXISTS products (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100),
    expiry_date     DATE,
    price_per_pack  DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    stock_quantity  INT NOT NULL DEFAULT 0,
    pack_size       DECIMAL(10,2) NOT NULL DEFAULT 1.00,
    unit            VARCHAR(20) NOT NULL DEFAULT 'piece',
    qr_code_data    VARCHAR(200),
    registered_by   INT,
    store_id        INT NOT NULL,
    FOREIGN KEY (registered_by) REFERENCES users(id),
    FOREIGN KEY (store_id) REFERENCES stores(id)
);

-- bills
-- bill_date: DATETIME → TIMESTAMP
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
);

-- purchases
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
);

-- alerts_log
-- recipient ENUM, method ENUM → VARCHAR + CHECK
CREATE TABLE IF NOT EXISTS alerts_log (
    id                 SERIAL PRIMARY KEY,
    customer_id        INT,
    product_id         INT,
    alert_sent_date    DATE,
    days_before_expiry INT,
    recipient          VARCHAR(10) NOT NULL DEFAULT 'both'
                           CHECK (recipient IN ('customer', 'admin', 'both')),
    method             VARCHAR(10) NOT NULL DEFAULT 'email'
                           CHECK (method IN ('email', 'sms')),
    FOREIGN KEY (customer_id) REFERENCES users(id),
    FOREIGN KEY (product_id)  REFERENCES products(id)
);

-- wastage_log
-- logged_at: DATETIME → TIMESTAMP
CREATE TABLE IF NOT EXISTS wastage_log (
    id         SERIAL PRIMARY KEY,
    product_id INT NOT NULL,
    quantity   INT NOT NULL,
    reason     VARCHAR(100) DEFAULT 'expired',
    logged_by  INT NULL,
    logged_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id),
    FOREIGN KEY (logged_by)  REFERENCES users(id)
);
