"""
Phase 1 - test_connection.py
Checks that Python can reach your Neon database using the details in .env.

Run from your project folder:
    python scripts/test_connection.py
"""

import os                      # lets us read environment variables
import psycopg2                # the PostgreSQL driver for Python
from dotenv import load_dotenv # reads the .env file into environment variables

# 1. Load the secrets from the .env file (it sits in the project root).
load_dotenv()

# 2. Read the single connection string we saved.
database_url = os.getenv("DATABASE_URL")

if not database_url:
    # Stop early with a clear message instead of a confusing error later.
    raise SystemExit("DATABASE_URL not found. Is your .env file in the project folder?")

try:
    # 3. Open a connection. Neon may take 1-3 seconds if the database was asleep.
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()  # a cursor is the object that sends SQL and receives results

    # 4. Ask the database who and where it is.
    cur.execute("SELECT version();")
    print("Connected! Server:", cur.fetchone()[0][:60], "...")

    # 5. Confirm our table exists and count its rows (should be 0 for now).
    cur.execute("SELECT COUNT(*) FROM bank_customers;")
    print("bank_customers rows:", cur.fetchone()[0])

    # 6. Always close what you open.
    cur.close()
    conn.close()
    print("Phase 1 check passed.")

except psycopg2.OperationalError as e:
    # Usually a wrong password, wrong host, or missing ?sslmode=require
    print("Could not connect. Check your .env values.\nDetails:", e)
except psycopg2.errors.UndefinedTable:
    print("Connected, but bank_customers does not exist. Run phase1_schema.sql in Neon first.")
