import os
import sys
from dotenv import load_dotenv
import pymysql
import psycopg2
import psycopg2.extras

def main():
    print("Loading .env environment variables...")
    base_dir = os.path.abspath(os.path.dirname(__file__))
    load_dotenv(os.path.join(base_dir, '.env'))

    mysql_host = os.environ.get('MYSQL_HOST', 'localhost')
    mysql_user = os.environ.get('MYSQL_USER', 'root')
    mysql_password = os.environ.get('MYSQL_PASSWORD')
    mysql_db = os.environ.get('MYSQL_DB', 'expiry_system')
    mysql_port_val = os.environ.get('MYSQLPORT') or os.environ.get('MYSQL_PORT')
    mysql_port = int(mysql_port_val) if mysql_port_val else 3306

    pg_url = os.environ.get('DATABASE_URL')

    if not pg_url:
        print("\n[ERROR] DATABASE_URL is not set in your .env file!")
        print("Please configure your Neon connection string first.")
        sys.exit(1)

    print("\n--- Connecting to MySQL Database ---")
    print(f"Host:     {mysql_host}")
    print(f"Port:     {mysql_port}")
    print(f"Database: {mysql_db}")
    try:
        mysql_conn = pymysql.connect(
            host=mysql_host,
            port=mysql_port,
            user=mysql_user,
            password=mysql_password,
            database=mysql_db,
            cursorclass=pymysql.cursors.DictCursor
        )
        print("MySQL connected successfully!")
    except Exception as e:
        print(f"MySQL connection failed: {e}")
        sys.exit(1)

    print("\n--- Connecting to PostgreSQL Database ---")
    print(f"URL: {pg_url.split('@')[-1] if '@' in pg_url else pg_url} (credentials hidden)")
    try:
        pg_conn = psycopg2.connect(pg_url)
        pg_conn.autocommit = False
        print("PostgreSQL connected successfully!")
    except Exception as e:
        mysql_conn.close()
        print(f"PostgreSQL connection failed: {e}")
        sys.exit(1)

    tables = ['stores', 'users', 'products', 'bills', 'purchases', 'alerts_log', 'wastage_log']

    try:
        mysql_cur = mysql_conn.cursor()
        pg_cur = pg_conn.cursor()

        print("\n--- Disabling constraints and truncating target PostgreSQL tables ---")
        # Truncate all tables in PostgreSQL cascade to start fresh
        truncate_query = "TRUNCATE TABLE " + ", ".join([f'"{t}"' for t in tables]) + " CASCADE;"
        pg_cur.execute(truncate_query)
        pg_conn.commit()
        print("Target tables truncated successfully.")

        for table in tables:
            print(f"\nMigrating table '{table}'...")
            
            # Fetch data from MySQL
            mysql_cur.execute(f"SELECT * FROM `{table}`")
            rows = mysql_cur.fetchall()
            
            if not rows:
                print(f" - Table '{table}' is empty in MySQL. Skipping.")
                continue

            # Extract columns dynamically
            columns = list(rows[0].keys())
            col_str = ", ".join([f'"{c}"' for c in columns])
            placeholders = ", ".join(["%s"] * len(columns))
            insert_query = f'INSERT INTO "{table}" ({col_str}) VALUES ({placeholders})'

            # Insert into PostgreSQL
            print(f" - Copying {len(rows)} row(s)...")
            for row in rows:
                values = []
                for c in columns:
                    val = row[c]
                    # Convert 1/0 integers to True/False for Boolean fields in PostgreSQL
                    if c == 'is_active' and val is not None:
                        val = bool(val)
                    values.append(val)
                pg_cur.execute(insert_query, values)

            pg_conn.commit()
            print(f" - Table '{table}' migrated successfully.")

        print("\n--- Resetting PostgreSQL Primary Key Identity Sequences ---")
        for table in tables:
            try:
                # Get the sequence backing the identity column
                pg_cur.execute(f"SELECT pg_get_serial_sequence('\"{table}\"', 'id')")
                seq_res = pg_cur.fetchone()
                if seq_res and seq_res[0]:
                    seq_name = seq_res[0]
                    pg_cur.execute(f'SELECT setval(\'{seq_name}\', COALESCE(MAX(id), 1), true) FROM "{table}"')
                    new_val = pg_cur.fetchone()[0]
                    print(f" - Reset sequence for '{table}' to {new_val}")
                else:
                    # In case pg_get_serial_sequence returns None, try default format
                    seq_name = f"{table}_id_seq"
                    pg_cur.execute(f'SELECT setval(\'{seq_name}\', COALESCE(MAX(id), 1), true) FROM "{table}"')
                    new_val = pg_cur.fetchone()[0]
                    print(f" - Reset sequence for '{table}' to {new_val} (fallback)")
            except Exception as seq_err:
                # Sequence might not exist if table structure is custom
                pg_conn.rollback()
                print(f" - Warning: Could not reset sequence for '{table}': {seq_err}")
                continue

        pg_conn.commit()
        print("\n[SUCCESS] Data migration completed successfully without errors!")

    except Exception as e:
        pg_conn.rollback()
        print(f"\n[ERROR] Data migration failed: {e}")
    finally:
        mysql_cur.close()
        mysql_conn.close()
        pg_cur.close()
        pg_conn.close()

if __name__ == '__main__':
    main()
