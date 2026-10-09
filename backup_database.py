"""
SmartPlantCare - Database Backup Utility
Connects to PostgreSQL (Render) and saves a full backup copy to SQLite (database_backup.db).
Can be run manually anytime:
    python backup_database.py
"""

import os
import sqlite3
import psycopg2
from dotenv import load_dotenv
from decimal import Decimal
from datetime import datetime, date
import shutil

load_dotenv()

PG_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_EXTERNAL")
if not PG_URL:
    print("[Error] Neither DATABASE_URL nor DATABASE_URL_EXTERNAL is set in .env!")
    exit(1)

if PG_URL.startswith("postgres://"):
    PG_URL = PG_URL.replace("postgres://", "postgresql://", 1)

BACKUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database_backup.db")
LOCAL_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")

print("Connecting to PostgreSQL on Render...")
try:
    pg_conn = psycopg2.connect(PG_URL)
    pg_cur = pg_conn.cursor()
except Exception as e:
    print(f"[Error] Failed to connect to PostgreSQL: {e}")
    exit(1)

print(f"Opening SQLite backup file: {BACKUP_FILE}")
sq_conn = sqlite3.connect(BACKUP_FILE)
sq_conn.row_factory = sqlite3.Row
sq_cur = sq_conn.cursor()

def convert_val(val):
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    return val

def backup_table(table_name):
    # Fetch PostgreSQL columns and data
    pg_cur.execute(f"SELECT * FROM {table_name} ORDER BY id")
    pg_rows = pg_cur.fetchall()
    pg_cols = [d[0].lower() for d in pg_cur.description]

    # Fetch SQLite columns for this table
    sq_cur.execute(f"PRAGMA table_info({table_name})")
    sq_cols_info = sq_cur.fetchall()
    sq_cols = [c[1].lower() for c in sq_cols_info]

    if not sq_cols:
        print(f"Skipping {table_name}: not found in SQLite backup.")
        return

    # Add any missing columns to SQLite table
    for c in pg_cols:
        if c not in sq_cols:
            try:
                sq_cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {c} TEXT DEFAULT ''")
                sq_cols.append(c)
            except Exception:
                pass

    # Find common columns between PG and SQLite
    common_cols = [c for c in pg_cols if c in sq_cols]
    if not common_cols:
        return

    placeholders = ", ".join(["?"] * len(common_cols))
    cols_str = ", ".join(common_cols)
    sql_insert = f"INSERT INTO {table_name} ({cols_str}) VALUES ({placeholders})"

    sq_cur.execute(f"DELETE FROM {table_name}")
    for r in pg_rows:
        row_dict = {col: convert_val(val) for col, val in zip(pg_cols, r)}
        values = [row_dict.get(c) for c in common_cols]
        sq_cur.execute(sql_insert, values)

    print(f"Backed up {len(pg_rows)} rows for '{table_name}'.")

for tbl in ["users", "plants", "orders", "order_items", "cart", "wishlist"]:
    try:
        backup_table(tbl)
    except Exception as err:
        print(f"Notice while backing up {tbl}: {err}")

sq_conn.commit()
sq_conn.close()
pg_conn.close()

# Also sync database.db
shutil.copyfile(BACKUP_FILE, LOCAL_DB)

print(f"\n[Success] Full database backup successfully saved to:")
print(f"  -> {BACKUP_FILE}")
print(f"  -> {LOCAL_DB}")
