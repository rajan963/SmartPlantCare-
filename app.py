import os
import re
import json
import sqlite3
import random
import string
import csv
import io
import secrets
import hashlib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, date
from decimal import Decimal
from functools import wraps
import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    make_response,
    jsonify
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

try:
    import psycopg2
except ImportError:
    psycopg2 = None

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "plantcare-hub-secret-key-2026-plants")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")
BACKUP_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database_backup.db")
UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "avif"}
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "rajankumar01331@gmail.com").strip().lower()

# Email Delivery Configuration (Universal: Brevo API, Google Apps Script Relay, Gmail SMTP)
GMAIL_RELAY_URL = os.getenv("GMAIL_RELAY_URL", os.getenv("GMAIL_SCRIPT_URL", "")).strip()
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "").strip()
GMAIL_SENDER = os.getenv("GMAIL_SENDER", "rajandas9080@gmail.com").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
OTP_EXPIRY_MINUTES = 5
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ==============================================================================
# POSTGRESQL COMPATIBILITY WRAPPER (FOR RENDER PERSISTENT STORAGE)
# ==============================================================================
class PostgresRow(dict):
    """
    Transparent dict-like and index-accessible row wrapper matching sqlite3.Row.
    Converts Decimal to float and datetime/date to formatted string
    for full compatibility with Jinja2 templates and numerical calculations.
    """
    def __init__(self, description, row_tuple):
        super().__init__()
        conv_list = []
        if description and row_tuple:
            for col, val in zip(description, row_tuple):
                if isinstance(val, Decimal):
                    val = float(val)
                elif isinstance(val, (datetime, date)):
                    val = val.strftime("%Y-%m-%d %H:%M:%S")
                conv_list.append(val)
                self[col.name.lower()] = val
        self._tuple = tuple(conv_list)

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._tuple[item]
        if isinstance(item, str):
            return super().__getitem__(item.lower())
        return super().__getitem__(item)

    def get(self, item, default=None):
        if isinstance(item, int):
            try:
                return self._tuple[item]
            except IndexError:
                return default
        if isinstance(item, str):
            return super().get(item.lower(), default)
        return super().get(item, default)

    def __contains__(self, item):
        if isinstance(item, str):
            return super().__contains__(item.lower())
        return super().__contains__(item)


class PostgresCursorWrapper:
    """
    Wraps psycopg2 cursor to mirror sqlite3 cursor semantics:
    1. Returns self from execute() so cur.execute(...).fetchone() / .fetchall() works seamlessly.
    2. Converts ? parameter placeholders to %s.
    3. Converts SQLite scalar MAX(0, ...) to PostgreSQL GREATEST(0, ...).
    4. Converts SUBSTR(col.created_at, ...) to SUBSTR(col.created_at::text, ...).
    5. Converts INTEGER PRIMARY KEY AUTOINCREMENT to SERIAL PRIMARY KEY in DDL.
    6. Automatically appends RETURNING id on INSERT to populate cur.lastrowid.
    7. Safely handles/ignores SQLite PRAGMA statements.
    """
    def __init__(self, pg_conn):
        self._conn = pg_conn
        self._cur = pg_conn.cursor()
        self.lastrowid = None
        self._description = None

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    def _convert_query(self, query):
        q = query
        # 1. Skip SQLite PRAGMA commands
        if q.strip().upper().startswith("PRAGMA"):
            return "PRAGMA"
        # 2. Convert SQLite DDL AUTOINCREMENT to SERIAL PRIMARY KEY
        q = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'SERIAL PRIMARY KEY', q, flags=re.IGNORECASE)
        # 3. Convert SQLite scalar MAX(0, ...) to PostgreSQL GREATEST(0, ...)
        q = re.sub(r'\bMAX\s*\(\s*0\s*,', 'GREATEST(0,', q, flags=re.IGNORECASE)
        # 4. Cast timestamp to text for SUBSTR
        q = re.sub(r'\bSUBSTR\s*\(\s*([a-zA-Z0-9_]+\.created_at|created_at)\s*,', r'SUBSTR(\1::text,', q, flags=re.IGNORECASE)
        # 5. Convert ? placeholders to %s
        q = q.replace('?', '%s')
        return q

    def execute(self, query, params=None):
        clean_q = self._convert_query(query)
        if clean_q == "PRAGMA":
            return self

        is_insert = bool(re.search(r'^\s*INSERT\s+INTO\s+', clean_q, re.IGNORECASE))
        has_returning = 'RETURNING' in clean_q.upper()

        if is_insert and not has_returning:
            q_returning = f"{clean_q.rstrip().rstrip(';')} RETURNING id"
            try:
                formatted_params = tuple(params) if isinstance(params, (list, tuple)) else params
                if formatted_params is not None:
                    self._cur.execute(q_returning, formatted_params)
                else:
                    self._cur.execute(q_returning)
                row = self._cur.fetchone()
                self.lastrowid = row[0] if row else None
                self._description = self._cur.description
                return self
            except psycopg2.Error as err:
                if "id" in str(err).lower() and "does not exist" in str(err).lower():
                    self._conn.rollback()
                else:
                    raise

        formatted_params = tuple(params) if isinstance(params, (list, tuple)) else params
        if formatted_params is not None:
            self._cur.execute(clean_q, formatted_params)
        else:
            self._cur.execute(clean_q)
        self._description = self._cur.description
        return self

    def executemany(self, query, seq_of_params):
        clean_q = self._convert_query(query)
        if clean_q == "PRAGMA":
            return self
        self._cur.executemany(clean_q, seq_of_params)
        self._description = self._cur.description
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return PostgresRow(self._cur.description, row)

    def fetchall(self):
        rows = self._cur.fetchall()
        desc = self._cur.description
        return [PostgresRow(desc, r) for r in rows]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size) if size else self._cur.fetchmany()
        desc = self._cur.description
        return [PostgresRow(desc, r) for r in rows]

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class PostgresConnectionWrapper:
    """
    Wraps psycopg2 connection to mirror sqlite3 connection semantics.
    Supports execute(), commit(), rollback(), close(), and context management.
    """
    def __init__(self, pg_conn):
        self._conn = pg_conn
        self.row_factory = None

    def cursor(self):
        return PostgresCursorWrapper(self._conn)

    def execute(self, query, params=None):
        cur = self.cursor()
        return cur.execute(query, params)

    def executemany(self, query, seq_of_params):
        cur = self.cursor()
        return cur.executemany(query, seq_of_params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()


# ==============================================================================
# DATABASE CONNECTION & INITIALIZATION
# ==============================================================================
def get_db():
    """
    Returns database connection:
    - If DATABASE_URL or DATABASE_URL_EXTERNAL is present, connects to PostgreSQL
      on Render for permanent, persistent data storage.
    - Otherwise (or if PostgreSQL is unreachable), falls back to SQLite database.db.
    """
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_EXTERNAL")
    if db_url and psycopg2:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        try:
            pg_conn = psycopg2.connect(db_url, connect_timeout=15)
            return PostgresConnectionWrapper(pg_conn)
        except Exception as pg_err:
            print(f"[Database] Warning: PostgreSQL connection failed ({pg_err}). Falling back to local SQLite.")

    # Local SQLite Fallback
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def seed_from_sqlite_backup(cur):
    """
    Auto-seeds PostgreSQL from local SQLite database / backup
    if the PostgreSQL database is brand new and has 0 plants or users.
    """
    source_db = DB_PATH if os.path.exists(DB_PATH) else BACKUP_DB_PATH
    if not os.path.exists(source_db):
        return

    try:
        sq_conn = sqlite3.connect(source_db)
        sq_conn.row_factory = sqlite3.Row
        sq_cur = sq_conn.cursor()

        # 1. Seed Users
        sq_users = sq_cur.execute("SELECT * FROM users").fetchall()
        for u in sq_users:
            u_dict = dict(u)
            cur.execute("""
                INSERT INTO users (fullname, email, username, password, phone, address, city, pincode, is_admin, created_at, is_verified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (email) DO NOTHING
            """, (
                u_dict.get("fullname", ""), u_dict.get("email", ""), u_dict.get("username", ""),
                u_dict.get("password", ""), u_dict.get("phone", ""), u_dict.get("address", ""),
                u_dict.get("city", ""), u_dict.get("pincode", ""), u_dict.get("is_admin", 0),
                u_dict.get("created_at", ""), u_dict.get("is_verified", 1)
            ))

        # 2. Seed Plants
        sq_plants = sq_cur.execute("SELECT * FROM plants").fetchall()
        for p in sq_plants:
            p_dict = dict(p)
            cur.execute("""
                INSERT INTO plants (name, scientific_name, category, price, original_price, stock,
                                    water, sunlight, soil, description, image, featured, is_active,
                                    rating, reviews_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p_dict.get("name", ""), p_dict.get("scientific_name", ""), p_dict.get("category", "Flowering"),
                float(p_dict.get("price") or 249.0), float(p_dict.get("original_price") or 349.0),
                int(p_dict.get("stock") or 20), p_dict.get("water", ""), p_dict.get("sunlight", ""),
                p_dict.get("soil", ""), p_dict.get("description", ""), p_dict.get("image", "plantCareimage.jpeg"),
                int(p_dict.get("featured") or 0), int(p_dict.get("is_active") or 1),
                float(p_dict.get("rating") or 4.8), int(p_dict.get("reviews_count") or 14)
            ))

        sq_conn.close()
        print(f"[Database] Auto-seeded {len(sq_users)} users and {len(sq_plants)} plants into PostgreSQL.")
    except Exception as e:
        print(f"[Database] Auto-seed warning: {e}")


def init_db():
    conn = get_db()
    cur = conn.cursor()
    is_pg = isinstance(conn, PostgresConnectionWrapper)

    # 1. Users Table
    if is_pg:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            fullname TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            username TEXT,
            password TEXT NOT NULL,
            phone TEXT DEFAULT '',
            address TEXT DEFAULT '',
            city TEXT DEFAULT '',
            pincode TEXT DEFAULT '',
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT '',
            is_verified INTEGER DEFAULT 1
        )
        """)
        for col_def in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin INTEGER DEFAULT 0;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified INTEGER DEFAULT 1;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user';"
        ]:
            try:
                cur.execute(col_def)
            except Exception:
                pass
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fullname TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            username TEXT,
            password TEXT NOT NULL,
            phone TEXT DEFAULT '',
            address TEXT DEFAULT '',
            city TEXT DEFAULT '',
            pincode TEXT DEFAULT '',
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ''
        )
        """)

    # Mark designated admin email
    cur.execute("UPDATE users SET is_admin = 1 WHERE LOWER(email) = ?", (ADMIN_EMAIL,))

    # 2. Plants Table
    if is_pg:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS plants (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            scientific_name TEXT DEFAULT '',
            category TEXT NOT NULL,
            price REAL DEFAULT 249.0,
            original_price REAL DEFAULT 349.0,
            stock INTEGER DEFAULT 20,
            water TEXT DEFAULT '2-3 times per week',
            sunlight TEXT DEFAULT 'Bright indirect sunlight',
            soil TEXT DEFAULT 'Well-drained rich potting soil',
            description TEXT DEFAULT '',
            image TEXT DEFAULT 'plantCareimage.jpeg',
            featured INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            rating REAL DEFAULT 4.8,
            reviews_count INTEGER DEFAULT 14,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        for col_def in [
            "ALTER TABLE plants ADD COLUMN IF NOT EXISTS is_active INTEGER DEFAULT 1;",
            "ALTER TABLE plants ADD COLUMN IF NOT EXISTS rating REAL DEFAULT 4.8;",
            "ALTER TABLE plants ADD COLUMN IF NOT EXISTS reviews_count INTEGER DEFAULT 14;"
        ]:
            try:
                cur.execute(col_def)
            except Exception:
                pass
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS plants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            scientific_name TEXT DEFAULT '',
            category TEXT NOT NULL,
            price REAL DEFAULT 249.0,
            original_price REAL DEFAULT 349.0,
            stock INTEGER DEFAULT 20,
            water TEXT DEFAULT '2-3 times per week',
            sunlight TEXT DEFAULT 'Bright indirect sunlight',
            soil TEXT DEFAULT 'Well-drained rich potting soil',
            description TEXT DEFAULT '',
            image TEXT DEFAULT 'plantCareimage.jpeg',
            featured INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            rating REAL DEFAULT 4.8,
            reviews_count INTEGER DEFAULT 14,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

    # 3. Cart Table
    if is_pg:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS cart (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plant_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, plant_id)
        )
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS cart (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plant_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, plant_id)
        )
        """)

    # 4. Wishlist Table
    if is_pg:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS wishlist (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plant_id INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, plant_id)
        )
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS wishlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plant_id INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, plant_id)
        )
        """)

    # 5. Orders Table
    if is_pg:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            order_number TEXT UNIQUE NOT NULL,
            user_id INTEGER,
            user_email TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            customer_phone TEXT NOT NULL,
            shipping_address TEXT NOT NULL,
            city TEXT NOT NULL,
            pincode TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'Cash on Delivery',
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            subtotal REAL NOT NULL DEFAULT 0.0,
            shipping_fee REAL NOT NULL DEFAULT 0.0,
            total_amount REAL NOT NULL DEFAULT 0.0,
            order_status TEXT NOT NULL DEFAULT 'Pending',
            admin_notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            user_id INTEGER,
            user_email TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            customer_phone TEXT NOT NULL,
            shipping_address TEXT NOT NULL,
            city TEXT NOT NULL,
            pincode TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'Cash on Delivery',
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            subtotal REAL NOT NULL DEFAULT 0.0,
            shipping_fee REAL NOT NULL DEFAULT 0.0,
            total_amount REAL NOT NULL DEFAULT 0.0,
            order_status TEXT NOT NULL DEFAULT 'Pending',
            admin_notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

    # 6. Order Items Table
    if is_pg:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL,
            plant_id INTEGER NOT NULL,
            plant_name TEXT NOT NULL,
            plant_image TEXT DEFAULT '',
            price REAL NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            subtotal REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        )
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            plant_id INTEGER NOT NULL,
            plant_name TEXT NOT NULL,
            plant_image TEXT DEFAULT '',
            price REAL NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            subtotal REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        )
        """)

    # Auto-seed and reset sequences for PostgreSQL
    if is_pg:
        try:
            plant_cnt_row = cur.execute("SELECT COUNT(*) FROM plants").fetchone()
            if plant_cnt_row and plant_cnt_row[0] == 0:
                seed_from_sqlite_backup(cur)
        except Exception as seed_check_err:
            print(f"[Database] Seed check notice: {seed_check_err}")

        for tbl in ['users', 'plants', 'orders', 'order_items', 'cart', 'wishlist']:
            try:
                cur.execute(f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), coalesce((SELECT MAX(id) FROM {tbl}), 1));")
            except Exception:
                pass

    conn.commit()
    conn.close()


init_db()


# ==============================================================================
# HELPERS & DECORATORS
# ==============================================================================
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def verify_password(stored_password, provided_password):
    if not stored_password or not provided_password:
        return False
    # 1. Try Werkzeug password hash verification (scrypt, pbkdf2, argon2, sha256)
    try:
        if check_password_hash(stored_password, provided_password):
            return True
        if check_password_hash(stored_password, provided_password.strip()):
            return True
    except Exception:
        pass
    # 2. Plaintext comparison (exact and trimmed)
    p_clean = provided_password.strip()
    s_clean = stored_password.strip()
    return (
        stored_password == provided_password
        or s_clean == p_clean
        or stored_password == p_clean
        or s_clean == provided_password
    )


def is_admin():
    if "user" not in session:
        return False
    user_email = session["user"].strip().lower()
    if user_email == ADMIN_EMAIL:
        return True
    return bool(session.get("is_admin"))


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            flash("Administrator login required.", "warning")
            return redirect(url_for("login", next=request.path))
        if not is_admin():
            flash("Access restricted to store administrators.", "danger")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def inject_globals():
    cart_count = 0
    wishlist_count = 0

    if "user_id" in session:
        conn = get_db()
        cur = conn.cursor()
        c = cur.execute("SELECT SUM(quantity) FROM cart WHERE user_id = ?", (session["user_id"],)).fetchone()
        cart_count = c[0] if c and c[0] else 0

        w = cur.execute("SELECT COUNT(*) FROM wishlist WHERE user_id = ?", (session["user_id"],)).fetchone()
        wishlist_count = w[0] if w and w[0] else 0
        conn.close()
    else:
        guest_cart = session.get("guest_cart", {})
        cart_count = sum(guest_cart.values())
        guest_wishlist = session.get("guest_wishlist", [])
        wishlist_count = len(guest_wishlist)

    session["cart_count"] = cart_count
    session["wishlist_count"] = wishlist_count

    return {
        "cart_count": cart_count,
        "wishlist_count": wishlist_count,
        "is_admin_user": is_admin(),
        "admin_email": ADMIN_EMAIL
    }


def get_user_wishlist_ids():
    if "user_id" in session:
        conn = get_db()
        cur = conn.cursor()
        rows = cur.execute("SELECT plant_id FROM wishlist WHERE user_id = ?", (session["user_id"],)).fetchall()
        conn.close()
        return {r["plant_id"] for r in rows}
    return set(session.get("guest_wishlist", []))


# ==============================================================================
# AUTHENTICATION & OTP HELPERS
# ==============================================================================
def generate_otp():
    """Generate a secure 6-digit numeric OTP."""
    return f"{secrets.randbelow(1000000):06d}"


def hash_otp(otp):
    """Hash OTP with SHA-256 for secure session storage."""
    return hashlib.sha256(str(otp).strip().encode("utf-8")).hexdigest()


def send_gmail_relay_email(receiver_email, subject, html_content, text_content=None):
    """
    Sends email via Google Apps Script Web App over HTTPS (port 443).
    Runs directly from Gmail, bypasses cloud SMTP port blocks (Render),
    and sends to ANY user or admin email address worldwide without domain verification.
    """
    relay_url = (os.getenv("GMAIL_RELAY_URL") or os.getenv("GMAIL_SCRIPT_URL") or GMAIL_RELAY_URL or "").strip()
    if not relay_url:
        raise ValueError("GMAIL_RELAY_URL is not configured.")

    payload = {
        "to": receiver_email.strip(),
        "subject": subject,
        "html": html_content,
        "text": text_content or ""
    }
    # Google Apps Script redirects with 302 to script.googleusercontent.com
    resp = requests.post(relay_url, json=payload, timeout=25, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"Google Apps Script Relay failed ({resp.status_code}): {resp.text}")
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"Google Apps Script Relay error: {data.get('message')}")
    except (ValueError, json.JSONDecodeError):
        pass
    return True


def send_brevo_email(receiver_email, subject, html_content, text_content=None):
    """
    Sends transactional email via Brevo REST API over HTTPS port 443.
    Free tier allows 300 emails/day to ANY recipient without domain restriction.
    """
    api_key = (os.getenv("BREVO_API_KEY") or BREVO_API_KEY or "").strip()
    if not api_key:
        raise ValueError("BREVO_API_KEY is not configured.")

    # Candidate sender emails to try:
    explicit_sender = (os.getenv("BREVO_SENDER_EMAIL") or BREVO_SENDER_EMAIL or "").strip()
    if explicit_sender:
        candidate_senders = [explicit_sender]
    else:
        candidate_senders = []
        for s in [
            "plantcareh@gmail.com",
            (os.getenv("GMAIL_SENDER") or GMAIL_SENDER or "").strip(),
            (os.getenv("ADMIN_EMAIL") or ADMIN_EMAIL or "").strip(),
            "rajandas9080@gmail.com",
            "rajankumar01331@gmail.com"
        ]:
            if s and s not in candidate_senders:
                candidate_senders.append(s)

    sender_name = os.getenv("BREVO_SENDER_NAME", "PlantCare Hub").strip()
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json"
    }

    last_error = None
    for sender_email in candidate_senders:
        payload = {
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": receiver_email.strip()}],
            "subject": subject,
            "htmlContent": html_content
        }
        if text_content:
            payload["textContent"] = text_content

        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        if resp.status_code in (200, 201):
            return resp.json()

        last_error = f"Brevo API error ({resp.status_code}) for sender {sender_email}: {resp.text}"
        print(f"[Brevo API] Warning with sender {sender_email}: {resp.status_code} - {resp.text}")
        if resp.status_code == 401:
            break

    raise RuntimeError(last_error or "Brevo API email dispatch failed.")


def send_email_smtp(receiver_email, subject, html_content, text_content=None):
    """Deliver email directly and reliably via Gmail SMTP using app password."""
    sender = (os.getenv("GMAIL_SENDER") or GMAIL_SENDER or "").strip()
    app_pw = (os.getenv("GMAIL_APP_PASSWORD") or GMAIL_APP_PASSWORD or "").strip().replace(" ", "")

    if not sender or not app_pw:
        raise ValueError("GMAIL_SENDER or GMAIL_APP_PASSWORD is not configured.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"PlantCare Hub <{sender}>"
    msg["To"] = receiver_email.strip()

    if text_content:
        msg.attach(MIMEText(text_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    # Use 5 second timeout so cloud hosts (Render) where SMTP is blocked don't hang the worker
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=5) as server:
        server.login(sender, app_pw)
        server.sendmail(sender, [receiver_email.strip()], msg.as_string())

    return True


def send_otp_email_universal(receiver_email, subject, html_content, text_content=None, otp=None):
    """
    Universal smart email delivery:
    1. Tier 1 (Primary): Brevo REST API (BREVO_API_KEY) -> Delivers directly to ANY email (user or admin) over HTTPS port 443.
    2. Tier 2: Google Apps Script Web App Relay (GMAIL_RELAY_URL) -> Fallback HTTPS relay.
    3. Tier 3: Direct Gmail SMTP -> Delivers if port 465 is reachable.
    - Sends directly to receiver_clean ONLY. Never forwards another user's OTP to admin!
    """
    receiver_clean = receiver_email.strip()

    # Always log OTP in server console for tracking
    if otp:
        print("\n" + "=" * 60, flush=True)
        print(f">> [OTP DISPATCH] Recipient: {receiver_clean} | Code: {otp}", flush=True)
        print("=" * 60 + "\n", flush=True)

    errors = []

    # Tier 1 (Primary): Brevo REST API (HTTPS port 443 - free 300 emails/day to ANY recipient without domain restriction)
    brevo_key = (os.getenv("BREVO_API_KEY") or BREVO_API_KEY or "").strip()
    if brevo_key:
        try:
            send_brevo_email(receiver_clean, subject, html_content, text_content)
            print(f"[OTP Dispatch] Successfully sent OTP to {receiver_clean} via Brevo API.")
            return True
        except Exception as e:
            print(f"[OTP Dispatch] Brevo API error: {e}")
            errors.append(f"Brevo API: {e}")

    # Tier 2: Google Apps Script Relay (HTTPS port 443)
    relay_url = (os.getenv("GMAIL_RELAY_URL") or os.getenv("GMAIL_SCRIPT_URL") or GMAIL_RELAY_URL or "").strip()
    if relay_url:
        try:
            send_gmail_relay_email(receiver_clean, subject, html_content, text_content)
            print(f"[OTP Dispatch] Successfully sent OTP to {receiver_clean} via Google Apps Script Relay.")
            return True
        except Exception as e:
            print(f"[OTP Dispatch] Google Apps Script Relay error: {e}")
            errors.append(f"Google Apps Script Relay: {e}")

    # Tier 3: Direct Gmail SMTP (Localhost or unblocked hosts)
    gmail_sender = (os.getenv("GMAIL_SENDER") or GMAIL_SENDER or "").strip()
    gmail_pw = (os.getenv("GMAIL_APP_PASSWORD") or GMAIL_APP_PASSWORD or "").strip()
    if gmail_sender and gmail_pw:
        try:
            send_email_smtp(receiver_clean, subject, html_content, text_content)
            print(f"[OTP Dispatch] Successfully sent OTP to {receiver_clean} via Gmail SMTP.")
            return True
        except Exception as e:
            print(f"[OTP Dispatch] Gmail SMTP error: {e}")
            errors.append(f"Gmail SMTP: {e}")

    # If all failed:
    detail_msg = " | ".join(errors) if errors else "No email dispatch service configured."
    raise RuntimeError(detail_msg)


def send_login_otp_email(receiver_email, otp):
    """Send 6-digit OTP for login verification."""
    subject = "PlantCare Hub - Login Verification OTP"
    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 520px; margin: 0 auto; background: #07150c; color: #ffffff; border-radius: 16px; border: 1px solid #1fa348; overflow: hidden; padding: 32px 28px;">
        <div style="text-align: center; margin-bottom: 24px;">
            <h2 style="margin: 0; color: #19ff69; font-size: 26px; letter-spacing: 0.5px;">🌱 PlantCare Hub</h2>
            <p style="margin: 6px 0 0; color: #9bb0a2; font-size: 13px;">Smart Agriculture & Plant Care System</p>
        </div>
        <div style="background: rgba(25, 255, 105, 0.08); border: 1px solid rgba(25, 255, 105, 0.25); border-radius: 12px; padding: 24px; text-align: center; margin-bottom: 24px;">
            <p style="margin: 0 0 10px; color: #e2f0e7; font-size: 15px; font-weight: 600;">Your Login Verification Code:</p>
            <div style="font-size: 38px; font-weight: 800; letter-spacing: 10px; color: #19ff69; padding: 12px 0; font-family: monospace;">{otp}</div>
            <p style="margin: 10px 0 0; color: #8fa897; font-size: 12.5px;">⏱ Valid for 5 minutes. Do not share this code with anyone.</p>
        </div>
        <p style="color: #9bb0a2; font-size: 13px; line-height: 1.6; margin: 0 0 16px;">
            If you did not attempt to sign in to PlantCare Hub, please ignore this email or reset your password immediately.
        </p>
        <div style="border-top: 1px solid rgba(255, 255, 255, 0.1); padding-top: 16px; text-align: center; color: #6d8474; font-size: 11.5px;">
            © 2026 PlantCare Hub. All rights reserved.
        </div>
    </div>
    """
    text_content = (
        f"Hello,\n\n"
        f"Your PlantCare Hub login verification code is: {otp}\n\n"
        f"This OTP is valid for 5 minutes.\n\n"
        f"If you did not request this OTP, please ignore this email.\n\n"
        f"Regards,\nPlantCare Hub Team"
    )
    return send_otp_email_universal(receiver_email, subject, html_content, text_content, otp=otp)


def send_forgot_password_otp_email(receiver_email, otp):
    """Send 6-digit OTP for password reset."""
    subject = "PlantCare Hub - Password Reset OTP"
    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 520px; margin: 0 auto; background: #07150c; color: #ffffff; border-radius: 16px; border: 1px solid #1fa348; overflow: hidden; padding: 32px 28px;">
        <div style="text-align: center; margin-bottom: 24px;">
            <h2 style="margin: 0; color: #19ff69; font-size: 26px; letter-spacing: 0.5px;">🌱 PlantCare Hub</h2>
            <p style="margin: 6px 0 0; color: #9bb0a2; font-size: 13px;">Password Reset Verification</p>
        </div>
        <div style="background: rgba(25, 255, 105, 0.08); border: 1px solid rgba(25, 255, 105, 0.25); border-radius: 12px; padding: 24px; text-align: center; margin-bottom: 24px;">
            <p style="margin: 0 0 10px; color: #e2f0e7; font-size: 15px; font-weight: 600;">Your Password Reset Code:</p>
            <div style="font-size: 38px; font-weight: 800; letter-spacing: 10px; color: #19ff69; padding: 12px 0; font-family: monospace;">{otp}</div>
            <p style="margin: 10px 0 0; color: #8fa897; font-size: 12.5px;">⏱ Valid for 5 minutes. Enter this code to set a new password.</p>
        </div>
        <p style="color: #9bb0a2; font-size: 13px; line-height: 1.6; margin: 0 0 16px;">
            If you did not request a password reset, please ignore this email. Your account remains completely secure.
        </p>
        <div style="border-top: 1px solid rgba(255, 255, 255, 0.1); padding-top: 16px; text-align: center; color: #6d8474; font-size: 11.5px;">
            © 2026 PlantCare Hub. All rights reserved.
        </div>
    </div>
    """
    text_content = (
        f"Hello,\n\n"
        f"Your PlantCare Hub password reset code is: {otp}\n\n"
        f"This OTP is valid for 5 minutes.\n\n"
        f"If you did not request this OTP, please ignore this email.\n\n"
        f"Regards,\nPlantCare Hub Team"
    )
    return send_otp_email_universal(receiver_email, subject, html_content, text_content, otp=otp)



def merge_guest_cart_and_wishlist(user_id):
    """Transfers items added in guest session to the authenticated user account."""
    guest_cart = session.get("guest_cart", {})
    guest_wishlist = session.get("guest_wishlist", [])
    if not guest_cart and not guest_wishlist:
        return

    conn = get_db()
    cur = conn.cursor()

    for plant_id_str, qty in guest_cart.items():
        try:
            pid = int(plant_id_str)
            row = cur.execute("SELECT id, quantity FROM cart WHERE user_id = ? AND plant_id = ?", (user_id, pid)).fetchone()
            if row:
                cur.execute("UPDATE cart SET quantity = quantity + ? WHERE user_id = ? AND plant_id = ?", (qty, user_id, pid))
            else:
                cur.execute("INSERT INTO cart (user_id, plant_id, quantity) VALUES (?, ?, ?)", (user_id, pid, qty))
        except Exception:
            pass

    for pid in guest_wishlist:
        try:
            row = cur.execute("SELECT id FROM wishlist WHERE user_id = ? AND plant_id = ?", (user_id, int(pid))).fetchone()
            if not row:
                cur.execute("INSERT INTO wishlist (user_id, plant_id) VALUES (?, ?)", (user_id, int(pid)))
        except Exception:
            pass

    conn.commit()
    conn.close()
    session.pop("guest_cart", None)
    session.pop("guest_wishlist", None)


# ==============================================================================
# AUTHENTICATION ROUTES
# ==============================================================================
@app.route("/register", methods=["GET", "POST"])
def register():
    """New user registration."""
    if "user" in session:
        return redirect(url_for("home"))

    if request.method == "POST":
        fullname = (request.form.get("reg_user_fullname") or request.form.get("fullname") or "").strip()
        email = (request.form.get("reg_user_email") or request.form.get("email") or "").strip().lower()
        username = (request.form.get("reg_user_login") or request.form.get("username") or "").strip()
        password = request.form.get("reg_user_password") or request.form.get("password") or ""

        if not fullname or not email or not username or not password:
            return render_template("register.html", error="Please fill in all required fields.")

        if "@" not in email or "." not in email:
            return render_template("register.html", error="Please enter a valid Gmail / email address.")

        if len(password) < 6:
            return render_template("register.html", error="Password must contain at least 6 characters.")

        conn = get_db()
        cur = conn.cursor()

        # Check duplicate email
        existing_email = cur.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email,)).fetchone()
        if existing_email:
            conn.close()
            return render_template("register.html", error="An account with this Gmail address already exists! Please login.")

        # Check duplicate username
        existing_user = cur.execute("SELECT id FROM users WHERE LOWER(username) = ?", (username.lower(),)).fetchone()
        if existing_user:
            conn.close()
            return render_template("register.html", error="This username is already taken. Please choose another username.")

        hashed_password = generate_password_hash(password)
        is_admin_flag = 1 if email == ADMIN_EMAIL else 0
        created_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cur.execute(
            """
            INSERT INTO users (fullname, email, username, password, is_admin, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fullname, email, username, hashed_password, is_admin_flag, created_time)
        )
        conn.commit()
        conn.close()

        flash("Account created successfully! Please log in with your credentials to continue.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Step 1 of login: Verify Email & Password.
    Upon successful credentials verification, sends a 6-digit OTP to the user's Gmail
    and redirects to /verify-otp for two-factor authentication.
    """
    if "user" in session:
        if is_admin():
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("home"))

    if request.method == "POST":
        email = (request.form.get("auth_email") or request.form.get("email") or "").strip().lower()
        password = request.form.get("auth_password") or request.form.get("password") or ""

        if not email or not password:
            return render_template("login.html", error="Please enter both Gmail address and password.")

        conn = get_db()
        cur = conn.cursor()
        user = cur.execute("SELECT * FROM users WHERE LOWER(email) = ?", (email,)).fetchone()
        conn.close()

        if not user or not verify_password(user["password"], password):
            return render_template(
                "login.html",
                error="Invalid Gmail address or password. If you forgot your password, please click Forgot Password."
            )

        # Credentials valid: Generate OTP and send
        otp = generate_otp()

        # Store pending login session
        session["pending_user_id"] = user["id"]
        session["pending_email"] = user["email"]
        session["pending_fullname"] = user["fullname"]
        session["pending_is_admin"] = bool(user["is_admin"] or user["email"].lower() == ADMIN_EMAIL)
        session["login_otp_hash"] = hash_otp(otp)
        session["login_otp_expiry"] = (datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()

        try:
            send_login_otp_email(user["email"], otp)
        except Exception as e:
            print("OTP EMAIL WARNING:", e)

        flash(f"A 6-digit OTP code has been sent to {user['email']}. Please check your inbox or spam folder.", "info")
        return redirect(url_for("verify_otp"))

    return render_template("login.html")


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    """
    Step 2 of login: Verify 6-digit Gmail OTP.
    Once verified, logs the user into the website and sets session.
    """
    if "user" in session:
        return redirect(url_for("home"))

    pending_email = session.get("pending_email")
    saved_hash = session.get("login_otp_hash")
    otp_expiry = session.get("login_otp_expiry")

    if not pending_email or not saved_hash or not otp_expiry:
        flash("No pending OTP session found. Please login first.", "warning")
        return redirect(url_for("login"))

    if request.method == "POST":
        entered_otp = request.form.get("otp", "").strip()

        try:
            expiry_time = datetime.fromisoformat(otp_expiry)
        except (ValueError, TypeError):
            for k in ["pending_user_id", "pending_email", "pending_fullname", "pending_is_admin", "login_otp_hash", "login_otp_expiry"]:
                session.pop(k, None)
            flash("OTP session expired. Please login again.", "danger")
            return redirect(url_for("login"))

        if datetime.now() > expiry_time:
            for k in ["pending_user_id", "pending_email", "pending_fullname", "pending_is_admin", "login_otp_hash", "login_otp_expiry"]:
                session.pop(k, None)
            flash("OTP has expired (valid 5 minutes). Please login again to request a new code.", "danger")
            return redirect(url_for("login"))

        if hash_otp(entered_otp) != saved_hash:
            return render_template(
                "verify_otp.html",
                error="Invalid OTP code. Please enter the correct 6-digit code sent to your Gmail.",
                email=pending_email
            )

        # OTP verified successfully: Log user in
        user_id = session.get("pending_user_id")
        fullname = session.get("pending_fullname")
        is_admin_flag = session.get("pending_is_admin", False)

        session["user_id"] = user_id
        session["user"] = pending_email
        session["fullname"] = fullname
        session["is_admin"] = is_admin_flag

        merge_guest_cart_and_wishlist(user_id)

        # Clean pending authentication session
        for k in ["pending_user_id", "pending_email", "pending_fullname", "pending_is_admin", "login_otp_hash", "login_otp_expiry"]:
            session.pop(k, None)

        flash(f"Welcome back, {fullname}! You have successfully logged in.", "success")

        if is_admin_flag:
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("home"))

    return render_template("verify_otp.html", email=pending_email)


@app.route("/resend-otp")
def resend_otp():
    """Resend a new 6-digit OTP code to the pending user's Gmail."""
    pending_email = session.get("pending_email")
    if not pending_email:
        flash("Please login first to request an OTP.", "warning")
        return redirect(url_for("login"))

    otp = generate_otp()
    session["login_otp_hash"] = hash_otp(otp)
    session["login_otp_expiry"] = (datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()

    try:
        send_login_otp_email(pending_email, otp)
    except Exception as e:
        print("RESEND OTP WARNING:", e)

    flash(f"A fresh 6-digit OTP code has been sent to {pending_email}. Please check your inbox or spam folder.", "success")
    return redirect(url_for("verify_otp"))


# ==============================================================================
# FORGOT PASSWORD ROUTES
# ==============================================================================
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Initiate password reset by sending OTP to registered Gmail."""
    if request.method == "POST":
        email = (request.form.get("auth_email") or request.form.get("email") or "").strip().lower()

        conn = get_db()
        cur = conn.cursor()
        user = cur.execute("SELECT id, fullname, email FROM users WHERE LOWER(email) = ?", (email,)).fetchone()
        conn.close()

        if not user:
            return render_template(
                "login.html",
                forgot_mode=True,
                error="No account found with this Gmail address. Please check and try again."
            )

        otp = generate_otp()

        session["forgot_otp_email"] = user["email"]
        session["forgot_otp_hash"] = hash_otp(otp)
        session["forgot_otp_expiry"] = (datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()

        try:
            send_forgot_password_otp_email(user["email"], otp)
        except Exception as e:
            print("FORGOT PASSWORD OTP WARNING:", e)

        return render_template(
            "login.html",
            forgot_otp_mode=True,
            otp_email=user["email"],
            message=f"Password reset OTP sent successfully to {user['email']}. Please check your inbox or spam folder."
        )

    return render_template("login.html", forgot_mode=True)


@app.route("/forgot-password/verify", methods=["POST"])
def verify_forgot_password():
    """Verify OTP entered for password reset."""
    entered_otp = request.form.get("otp", "").strip()
    saved_hash = session.get("forgot_otp_hash")
    email = session.get("forgot_otp_email")
    expiry = session.get("forgot_otp_expiry")

    if not saved_hash or not email or not expiry:
        return render_template(
            "login.html",
            forgot_mode=True,
            error="OTP session expired. Please request a new OTP."
        )

    try:
        expiry_time = datetime.fromisoformat(expiry)
    except (ValueError, TypeError):
        for k in ["forgot_otp_hash", "forgot_otp_email", "forgot_otp_expiry", "forgot_verified"]:
            session.pop(k, None)
        return render_template(
            "login.html",
            forgot_mode=True,
            error="OTP expired. Please request a new OTP."
        )

    if datetime.now() > expiry_time:
        for k in ["forgot_otp_hash", "forgot_otp_email", "forgot_otp_expiry", "forgot_verified"]:
            session.pop(k, None)
        return render_template(
            "login.html",
            forgot_mode=True,
            error="OTP has expired (valid 5 minutes). Please request a new OTP."
        )

    if hash_otp(entered_otp) != saved_hash:
        return render_template(
            "login.html",
            forgot_otp_mode=True,
            otp_email=email,
            error="Invalid OTP. Please check your Gmail and try again."
        )

    # OTP verified for password reset
    session["forgot_verified"] = True
    return render_template(
        "login.html",
        reset_password_mode=True,
        otp_email=email
    )


@app.route("/forgot-password/reset", methods=["POST"])
def reset_password():
    """Update password after OTP verification."""
    if not session.get("forgot_verified"):
        return redirect(url_for("forgot_password"))

    email = session.get("forgot_otp_email")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not email:
        return redirect(url_for("forgot_password"))

    if not new_password:
        return render_template(
            "login.html",
            reset_password_mode=True,
            otp_email=email,
            error="Please enter a new password."
        )

    if len(new_password) < 6:
        return render_template(
            "login.html",
            reset_password_mode=True,
            otp_email=email,
            error="Password must contain at least 6 characters."
        )

    if new_password != confirm_password:
        return render_template(
            "login.html",
            reset_password_mode=True,
            otp_email=email,
            error="Passwords do not match. Please re-enter."
        )

    conn = get_db()
    cur = conn.cursor()
    hashed = generate_password_hash(new_password)
    cur.execute("UPDATE users SET password = ? WHERE LOWER(email) = ?", (hashed, email.lower()))
    conn.commit()
    conn.close()

    # Clear forgot-password session
    for k in ["forgot_otp_hash", "forgot_otp_email", "forgot_otp_expiry", "forgot_verified"]:
        session.pop(k, None)

    return render_template(
        "login.html",
        reset_success=True,
        message="Password updated successfully. Please login with your new password."
    )


@app.route("/dashboard")
@login_required
def dashboard():
    """Universal dashboard redirect."""
    if is_admin():
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    """Clear session and log user out."""
    session.clear()
    flash("You have been logged out successfully.", "info")
    return redirect(url_for("home"))



# ==============================================================================
# STOREFRONT ROUTES (CUSTOMER FACING)
# ==============================================================================
@app.route("/")
def home():
    conn = get_db()
    cur = conn.cursor()
    featured_plants = cur.execute(
        "SELECT * FROM plants WHERE is_active = 1 ORDER BY id DESC LIMIT 8"
    ).fetchall()
    wishlist_ids = get_user_wishlist_ids()
    conn.close()
    return render_template("index.html", featured_plants=featured_plants, wishlist_ids=wishlist_ids)




@app.route("/shop")
def shop():
    category = request.args.get("category", "").strip()
    search = request.args.get("search", "").strip()
    sort = request.args.get("sort", "newest").strip()

    conn = get_db()
    cur = conn.cursor()

    query = "SELECT * FROM plants WHERE is_active = 1"
    params = []

    if category:
        query += " AND LOWER(category) = LOWER(?)"
        params.append(category)

    if search:
        query += " AND (LOWER(name) LIKE ? OR LOWER(scientific_name) LIKE ? OR LOWER(description) LIKE ?)"
        wildcard = f"%{search.lower()}%"
        params.extend([wildcard, wildcard, wildcard])

    if sort == "price_asc":
        query += " ORDER BY price ASC"
    elif sort == "price_desc":
        query += " ORDER BY price DESC"
    elif sort == "name_asc":
        query += " ORDER BY name ASC"
    else:
        query += " ORDER BY id DESC"

    plants = cur.execute(query, params).fetchall()

    all_categories = cur.execute(
        "SELECT category, COUNT(*) as count FROM plants WHERE is_active = 1 GROUP BY category ORDER BY count DESC"
    ).fetchall()

    wishlist_ids = get_user_wishlist_ids()
    conn.close()

    return render_template(
        "shop.html",
        plants=plants,
        current_category=category,
        search=search,
        sort=sort,
        all_categories=all_categories,
        wishlist_ids=wishlist_ids
    )


@app.route("/plant/<int:id>")
def plant_details(id):
    conn = get_db()
    cur = conn.cursor()

    plant = cur.execute("SELECT * FROM plants WHERE id = ?", (id,)).fetchone()
    if not plant:
        conn.close()
        flash("Plant not found in our nursery catalog.", "error")
        return redirect(url_for("shop"))

    related_plants = cur.execute(
        "SELECT * FROM plants WHERE category = ? AND id != ? AND is_active = 1 LIMIT 4",
        (plant["category"], id)
    ).fetchall()

    wishlist_ids = get_user_wishlist_ids()
    conn.close()

    return render_template(
        "plant_details.html",
        plant=plant,
        related_plants=related_plants,
        wishlist_ids=wishlist_ids
    )


@app.route("/categories")
def categories():
    conn = get_db()
    cur = conn.cursor()

    cats = cur.execute(
        "SELECT category, COUNT(*) as count FROM plants WHERE is_active = 1 GROUP BY category ORDER BY count DESC"
    ).fetchall()
    conn.close()

    meta = {
        "Flowering": {"icon": "🌸", "desc": "Fragrant and vibrant blooming roses, lilies, hibiscus, and jasmines to enrich garden color."},
        "Medicinal": {"icon": "🌿", "desc": "Sacred Holy Tulsi, soothing Aloe Vera, and organic healing herbs for family wellness."},
        "Indoor": {"icon": "🪴", "desc": "Lush foliage plants that flourish under indirect light and elevate room aesthetic."},
        "Air Purifying": {"icon": "🍃", "desc": "Natural botanical detoxifiers certified to purify indoor air toxins and boost oxygen."},
        "Succulent": {"icon": "🌵", "desc": "Hardy, sculptural plants requiring minimal watering and thriving in sunlit windows."},
        "Outdoor": {"icon": "🌳", "desc": "Sun-loving hearty shrubs, decorative hedges, and terrace flowering plants."}
    }

    categories_data = []
    for c in cats:
        cat_name = c["category"]
        info = meta.get(cat_name, {"icon": "🌱", "desc": "Healthy and vibrant nursery cultivated plants."})
        categories_data.append({
            "name": cat_name,
            "count": c["count"],
            "icon": info["icon"],
            "desc": info["desc"]
        })

    # Add any standard categories not yet populated with count 0
    for name, info in meta.items():
        if not any(cd["name"].lower() == name.lower() for cd in categories_data):
            categories_data.append({
                "name": name,
                "count": 0,
                "icon": info["icon"],
                "desc": info["desc"]
            })

    return render_template("categories.html", categories_data=categories_data)


# ==============================================================================
# PLANTCARE AI - CHATBOT & PLANT HEALTH DIAGNOSIS
# ==============================================================================
PLANTCARE_AI_SYSTEM_PROMPT = """You are PlantCare AI 🌱, an expert botanical and gardening AI assistant for PlantCare Hub.

YOUR DOMAIN & EXPERTISE:
You specialize EXCLUSIVELY in plants, gardening, plant care, plant diseases, watering, sunlight, soil, fertilizers, pest control, indoor/outdoor plants, seeds, and home/balcony gardening.
Questions can be asked in English, Hindi, or Hinglish (for example: "Mere plant ke leaves yellow kyun ho rahe hain?", "Plant ko kitna paani dena chahiye?", "Tulsi soil mix", "Insects spray", "Money plant ki dekhbhal").
ALWAYS answer all plant and gardening questions enthusiastically, warmly, and helpfully!

STRICT GUARDRAIL FOR UNRELATED TOPICS:
If and ONLY if the user asks about completely unrelated, non-plant topics (such as movies, cinema, cricket, sports, politics, celebrities, tech/coding, news, or general non-nature subjects), you MUST decline politely by replying:
"I am PlantCare AI 🌱. I can help you with plant care, plant diseases, watering, sunlight, soil, fertilizers, and gardening questions. Please ask me anything related to plants!"
(If the user's question was in Hindi/Hinglish, you may add: "कृपया पौधों और बागवानी (gardening) से जुड़े सवाल पूछें 🌱")
Never answer off-topic queries under any circumstance.

GUIDELINES FOR ANSWERING:
1. Language: Answer in the same language the user used (Hindi, Hinglish, or English). Keep your language simple, friendly, and easy to understand.
2. Step-by-Step Simple Solutions: Always organize remedies, diagnoses, and care instructions into clear numbered steps (1, 2, 3...) so anyone can follow them easily at home.
3. Plant Care & Problems:
   - Watering: Explain the finger-moisture check (mitti me 1-2 inch ungli daal kar check karein), proper drainage, signs of overwatering vs underwatering.
   - Sunlight: Specify direct sunlight, bright indirect light, or shade requirements.
   - Fertilizer: Recommend feeding schedules, organic fertilizers (vermicompost, cow dung manure, mustard cake liquid / sarson khali, banana peel water). Warn against over-fertilizing during dormancy/winters.
   - Growth Boost: Suggest aeration (gudai), pruning dead/dry leaves, wiping dust off foliage, repotting when root-bound.
   - Yellow Leaves & Brown Spots: Identify causes (overwatering, sunburn, low humidity, nutrient deficiency, fungal spots) and give exact remedies.
   - Insects/Pests: For mealybugs, aphids, spider mites, recommend organic remedies first (neem oil spray with mild liquid soap in water, water jet spray) before harsh chemicals.
   - Detailed Plant Info: Provide exact care tips for Money Plant (pothos), Tulsi (holy basil - well drained holy soil mix), Aloe Vera (succulent mix, avoid overwatering), Snake Plant, Peace Lily, etc.
   - Plant Recommendations: Suggest suitable plants for indoor spaces, bedrooms, balconies, low-light corners, or air-purification.
4. Follow-up Questions: Remember conversation context and encourage follow-ups.
5. Safety & Photo Diagnosis:
   - Always prioritize organic/safe methods. If suggesting chemical pesticides or fertilizers, provide essential safety warnings (wear gloves, mask, proper dilution, keep away from pets and children).
   - When diagnosing from a photo or symptom description, describe what you visually observe and mention that exact diagnosis may depend on checking soil moisture, pot drainage, and looking underneath leaves.
"""

@app.route("/plantcare-ai")
def plantcare_ai():
    return render_template("plantcare_ai.html")


@app.route("/api/plantcare-ai/chat", methods=["POST"])
def plantcare_ai_chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    image_data = data.get("image")  # base64 data URL: data:image/...;base64,...
    history = data.get("history") or []

    if not user_message and not image_data:
        return jsonify({"error": "Please provide a question or upload a plant photo."}), 400

    api_key = OPENROUTER_API_KEY
    if not api_key:
        return jsonify({"error": "OpenRouter API Key is not configured in .env file."}), 500

    # Build messages list
    messages = [{"role": "system", "content": PLANTCARE_AI_SYSTEM_PROMPT}]

    # Include recent conversation turns (up to 6)
    if isinstance(history, list):
        for turn in history[-6:]:
            if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") and turn.get("content"):
                text_content = turn.get("content")
                if isinstance(text_content, str) and text_content.strip():
                    messages.append({
                        "role": turn["role"],
                        "content": text_content.strip()
                    })

    # Add current user prompt
    if image_data:
        prompt_text = user_message if user_message else "Please diagnose this plant from the image: identify the plant if possible, evaluate its health, detect any yellowing, brown spots, pest attacks or diseases, and provide clear step-by-step care and remedies."
        user_content = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": image_data}}
        ]
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": user_message})

    # Models list: primary and fallback
    models_to_try = ["google/gemini-2.5-flash", "google/gemini-2.5-flash-lite"]
    last_error = None

    for model_name in models_to_try:
        try:
            req_payload = {
                "model": model_name,
                "max_tokens": 1200,
                "temperature": 0.7,
                "messages": messages
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://plantcarehub.com",
                "X-Title": "PlantCare Hub AI"
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=req_payload,
                timeout=45
            )

            if resp.status_code == 200:
                res_data = resp.json()
                reply = res_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if reply:
                    return jsonify({"reply": reply})
            else:
                last_error = f"API Error ({resp.status_code}): {resp.text}"
                print(f"[PlantCare AI] Model {model_name} failed: {resp.status_code} - {resp.text[:200]}")
        except Exception as e:
            last_error = str(e)
            print(f"[PlantCare AI] Exception calling {model_name}: {e}")

    return jsonify({
        "error": "I couldn't process your request right now. Please check your internet connection or try again in a moment.",
        "details": last_error
    }), 502


# ==============================================================================
# CART & WISHLIST MANAGEMENT
# ==============================================================================
@app.route("/cart")
def cart():
    cart_items = []
    subtotal = 0.0

    conn = get_db()
    cur = conn.cursor()

    if "user_id" in session:
        rows = cur.execute("""
            SELECT c.id as cart_id, c.quantity, p.id as plant_id, p.name, p.category, p.price, p.image, p.stock
            FROM cart c
            JOIN plants p ON c.plant_id = p.id
            WHERE c.user_id = ?
        """, (session["user_id"],)).fetchall()
        for r in rows:
            item_dict = dict(r)
            subtotal += item_dict["price"] * item_dict["quantity"]
            cart_items.append(item_dict)
    else:
        guest_cart = session.get("guest_cart", {})
        for pid_str, qty in guest_cart.items():
            plant = cur.execute("SELECT id, name, category, price, image, stock FROM plants WHERE id = ?", (int(pid_str),)).fetchone()
            if plant:
                item_dict = dict(plant)
                item_dict["plant_id"] = plant["id"]
                item_dict["quantity"] = qty
                subtotal += plant["price"] * qty
                cart_items.append(item_dict)

    conn.close()

    shipping_fee = 0.0 if (subtotal >= 499.0 or subtotal == 0) else 50.0
    total = subtotal + shipping_fee

    return render_template(
        "cart.html",
        cart_items=cart_items,
        subtotal=subtotal,
        shipping_fee=shipping_fee,
        total=total
    )


@app.route("/add-to-cart/<int:id>", methods=["POST"])
def add_to_cart(id):
    try:
        quantity = max(1, int(request.form.get("quantity", 1)))
    except ValueError:
        quantity = 1

    buy_now = request.form.get("buy_now") == "1"

    conn = get_db()
    cur = conn.cursor()
    plant = cur.execute("SELECT * FROM plants WHERE id = ? AND is_active = 1", (id,)).fetchone()

    if not plant:
        conn.close()
        flash("Plant not found.", "error")
        return redirect(url_for("shop"))

    if plant["stock"] < 1:
        conn.close()
        flash(f"Sorry, {plant['name']} is currently out of stock.", "error")
        return redirect(url_for("plant_details", id=id))

    quantity = min(quantity, plant["stock"])

    if "user_id" in session:
        existing = cur.execute(
            "SELECT id, quantity FROM cart WHERE user_id = ? AND plant_id = ?",
            (session["user_id"], id)
        ).fetchone()

        if existing:
            new_qty = min(plant["stock"], existing["quantity"] + quantity)
            cur.execute("UPDATE cart SET quantity = ? WHERE id = ?", (new_qty, existing["id"]))
        else:
            cur.execute(
                "INSERT INTO cart (user_id, plant_id, quantity) VALUES (?, ?, ?)",
                (session["user_id"], id, quantity)
            )
        conn.commit()
    else:
        guest_cart = session.get("guest_cart", {})
        pid_str = str(id)
        current = guest_cart.get(pid_str, 0)
        guest_cart[pid_str] = min(plant["stock"], current + quantity)
        session["guest_cart"] = guest_cart

    conn.close()

    if buy_now:
        return redirect(url_for("checkout"))

    flash(f"Added {quantity}x {plant['name']} to your cart!", "success")
    return redirect(request.referrer or url_for("cart"))


@app.route("/update-cart/<int:id>", methods=["POST"])
def update_cart(id):
    try:
        new_qty = int(request.form.get("quantity", 1))
    except ValueError:
        new_qty = 1

    conn = get_db()
    cur = conn.cursor()

    if new_qty <= 0:
        if "user_id" in session:
            cur.execute("DELETE FROM cart WHERE user_id = ? AND plant_id = ?", (session["user_id"], id))
            conn.commit()
        else:
            guest_cart = session.get("guest_cart", {})
            guest_cart.pop(str(id), None)
            session["guest_cart"] = guest_cart
        flash("Item removed from cart.", "info")
    else:
        plant = cur.execute("SELECT stock, name FROM plants WHERE id = ?", (id,)).fetchone()
        if plant:
            capped_qty = min(plant["stock"], new_qty)
            if "user_id" in session:
                cur.execute(
                    "UPDATE cart SET quantity = ? WHERE user_id = ? AND plant_id = ?",
                    (capped_qty, session["user_id"], id)
                )
                conn.commit()
            else:
                guest_cart = session.get("guest_cart", {})
                guest_cart[str(id)] = capped_qty
                session["guest_cart"] = guest_cart

    conn.close()
    return redirect(url_for("cart"))


@app.route("/remove-from-cart/<int:id>")
def remove_from_cart(id):
    conn = get_db()
    cur = conn.cursor()

    if "user_id" in session:
        cur.execute("DELETE FROM cart WHERE user_id = ? AND plant_id = ?", (session["user_id"], id))
        conn.commit()
    else:
        guest_cart = session.get("guest_cart", {})
        guest_cart.pop(str(id), None)
        session["guest_cart"] = guest_cart

    conn.close()
    flash("Item removed from cart.", "info")
    return redirect(url_for("cart"))


@app.route("/clear-cart")
def clear_cart():
    if "user_id" in session:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM cart WHERE user_id = ?", (session["user_id"],))
        conn.commit()
        conn.close()
    else:
        session.pop("guest_cart", None)

    flash("Cart cleared.", "info")
    return redirect(url_for("cart"))


@app.route("/wishlist")
def wishlist():
    conn = get_db()
    cur = conn.cursor()
    wishlist_items = []

    if "user_id" in session:
        wishlist_items = cur.execute("""
            SELECT p.* FROM wishlist w
            JOIN plants p ON w.plant_id = p.id
            WHERE w.user_id = ? AND p.is_active = 1
            ORDER BY w.id DESC
        """, (session["user_id"],)).fetchall()
    else:
        guest_wishlist = session.get("guest_wishlist", [])
        if guest_wishlist:
            placeholders = ",".join("?" * len(guest_wishlist))
            wishlist_items = cur.execute(
                f"SELECT * FROM plants WHERE id IN ({placeholders}) AND is_active = 1",
                guest_wishlist
            ).fetchall()

    conn.close()
    return render_template("wishlist.html", wishlist_items=wishlist_items)


@app.route("/toggle-wishlist/<int:id>")
def toggle_wishlist(id):
    conn = get_db()
    cur = conn.cursor()
    plant = cur.execute("SELECT name FROM plants WHERE id = ?", (id,)).fetchone()

    if not plant:
        conn.close()
        flash("Plant not found.", "error")
        return redirect(request.referrer or url_for("shop"))

    plant_name = plant["name"]

    if "user_id" in session:
        existing = cur.execute(
            "SELECT id FROM wishlist WHERE user_id = ? AND plant_id = ?",
            (session["user_id"], id)
        ).fetchone()

        if existing:
            cur.execute("DELETE FROM wishlist WHERE id = ?", (existing["id"],))
            flash(f"Removed {plant_name} from your wishlist.", "info")
        else:
            cur.execute("INSERT INTO wishlist (user_id, plant_id) VALUES (?, ?)", (session["user_id"], id))
            flash(f"Added {plant_name} to your wishlist!", "success")
        conn.commit()
    else:
        guest_wishlist = session.get("guest_wishlist", [])
        if id in guest_wishlist:
            guest_wishlist.remove(id)
            flash(f"Removed {plant_name} from your wishlist.", "info")
        else:
            guest_wishlist.append(id)
            flash(f"Added {plant_name} to your wishlist!", "success")
        session["guest_wishlist"] = guest_wishlist

    conn.close()
    return redirect(request.referrer or url_for("shop"))


# ==============================================================================
# CHECKOUT & ORDERS (USER PURCHASE REQUEST WORKFLOW)
# ==============================================================================
@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    conn = get_db()
    cur = conn.cursor()

    # Get cart items
    cart_items = cur.execute("""
        SELECT c.quantity, p.id as plant_id, p.name, p.price, p.image, p.stock
        FROM cart c
        JOIN plants p ON c.plant_id = p.id
        WHERE c.user_id = ?
    """, (session["user_id"],)).fetchall()

    if not cart_items:
        conn.close()
        flash("Your cart is empty. Add plants before checking out.", "warning")
        return redirect(url_for("shop"))

    subtotal = sum(item["price"] * item["quantity"] for item in cart_items)
    shipping_fee = 0.0 if subtotal >= 499.0 else 50.0
    total = subtotal + shipping_fee

    user = cur.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    if request.method == "POST":
        fullname = request.form.get("fullname", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()
        pincode = request.form.get("pincode", "").strip()
        payment_method = request.form.get("payment_method", "Cash on Delivery").strip()

        if not fullname or not phone or not address or not city or not pincode:
            conn.close()
            flash("Please complete all shipping address fields.", "error")
            return render_template(
                "checkout.html",
                user=user,
                cart_items=cart_items,
                subtotal=subtotal,
                shipping_fee=shipping_fee,
                total=total
            )

        # Generate unique human-readable order number
        order_date_str = datetime.now().strftime("%Y%m%d")
        random_suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        order_number = f"ORD-{order_date_str}-{random_suffix}"

        # Insert Order with status 'Pending' (waiting for admin to accept!)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            INSERT INTO orders (
                order_number, user_id, user_email, customer_name, customer_phone,
                shipping_address, city, pincode, payment_method, payment_status,
                subtotal, shipping_fee, total_amount, order_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)
        """, (
            order_number, session["user_id"], session["user"], fullname, phone,
            address, city, pincode, payment_method,
            "Paid" if "Online" in payment_method else "Pending",
            subtotal, shipping_fee, total, now_str, now_str
        ))
        order_id = cur.lastrowid

        # Insert Order Items & decrease stock
        for item in cart_items:
            item_subtotal = item["price"] * item["quantity"]
            cur.execute("""
                INSERT INTO order_items (
                    order_id, plant_id, plant_name, plant_image, price, quantity, subtotal
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id, item["plant_id"], item["name"], item["image"],
                item["price"], item["quantity"], item_subtotal
            ))
            # Deduct stock
            cur.execute("UPDATE plants SET stock = MAX(0, stock - ?) WHERE id = ?", (item["quantity"], item["plant_id"]))

        # Clear cart
        cur.execute("DELETE FROM cart WHERE user_id = ?", (session["user_id"],))

        # Save default address in user profile
        cur.execute("""
            UPDATE users SET fullname = ?, phone = ?, address = ?, city = ?, pincode = ?
            WHERE id = ?
        """, (fullname, phone, address, city, pincode, session["user_id"]))

        conn.commit()
        conn.close()

        flash("Plant order placed! Your request is pending nursery admin acceptance.", "success")
        return redirect(url_for("order_success", order_number=order_number))

    conn.close()
    return render_template(
        "checkout.html",
        user=user,
        cart_items=cart_items,
        subtotal=subtotal,
        shipping_fee=shipping_fee,
        total=total
    )


@app.route("/order-success/<order_number>")
@login_required
def order_success(order_number):
    conn = get_db()
    cur = conn.cursor()

    order = cur.execute(
        "SELECT * FROM orders WHERE order_number = ? AND user_id = ?",
        (order_number, session["user_id"])
    ).fetchone()

    if not order:
        conn.close()
        flash("Order not found.", "error")
        return redirect(url_for("home"))

    order_items = cur.execute(
        "SELECT * FROM order_items WHERE order_id = ?",
        (order["id"],)
    ).fetchall()
    conn.close()

    return render_template("order_success.html", order=order, order_items=order_items)


@app.route("/my-orders")
@login_required
def my_orders():
    conn = get_db()
    cur = conn.cursor()

    orders_rows = cur.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC",
        (session["user_id"],)
    ).fetchall()

    orders = []
    for r in orders_rows:
        order_dict = dict(r)
        items = cur.execute(
            "SELECT * FROM order_items WHERE order_id = ?",
            (r["id"],)
        ).fetchall()
        order_dict["order_items"] = [dict(it) for it in items]
        orders.append(order_dict)

    conn.close()
    return render_template("my_orders.html", orders=orders)


@app.route("/cancel-order/<int:order_id>", methods=["GET", "POST"])
@login_required
def cancel_order(order_id):
    conn = get_db()
    cur = conn.cursor()

    order = cur.execute(
        "SELECT * FROM orders WHERE id = ? AND user_id = ?",
        (order_id, session["user_id"])
    ).fetchone()

    if not order:
        conn.close()
        flash("Order not found.", "error")
        return redirect(url_for("my_orders"))

    if order["order_status"] != "Pending":
        conn.close()
        flash("This order has already been processed and cannot be cancelled directly.", "warning")
        return redirect(url_for("my_orders"))

    # Restore inventory stock
    items = cur.execute("SELECT plant_id, quantity FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    for it in items:
        cur.execute("UPDATE plants SET stock = stock + ? WHERE id = ?", (it["quantity"], it["plant_id"]))

    cur.execute("UPDATE orders SET order_status = 'Cancelled' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    flash("Your order has been cancelled.", "info")
    return redirect(url_for("my_orders"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        fullname = request.form.get("fullname", "").strip()
        username = request.form.get("username", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()
        pincode = request.form.get("pincode", "").strip()

        cur.execute("""
            UPDATE users SET fullname = ?, username = ?, phone = ?, address = ?, city = ?, pincode = ?
            WHERE id = ?
        """, (fullname, username, phone, address, city, pincode, session["user_id"]))
        conn.commit()

        session["fullname"] = fullname
        flash("Profile information updated successfully!", "success")

    user = cur.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    order_count = cur.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (session["user_id"],)).fetchone()[0]
    wishlist_count = cur.execute("SELECT COUNT(*) FROM wishlist WHERE user_id = ?", (session["user_id"],)).fetchone()[0]
    conn.close()

    return render_template(
        "profile.html",
        user=user,
        order_count=order_count,
        wishlist_count=wishlist_count
    )


# ==============================================================================
# ADMIN PANEL ROUTES (ADMIN DASHBOARD, PLANTS, ACCEPT ORDERS, SALES, USERS)
# ==============================================================================
@app.route("/admin")
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    conn = get_db()
    cur = conn.cursor()

    total_revenue_res = cur.execute(
        "SELECT SUM(total_amount) FROM orders WHERE order_status IN ('Accepted', 'Shipped', 'Delivered')"
    ).fetchone()[0]
    total_revenue = total_revenue_res if total_revenue_res else 0.0

    total_orders = cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    pending_count = cur.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'Pending'").fetchone()[0]
    total_plants = cur.execute("SELECT COUNT(*) FROM plants WHERE is_active = 1").fetchone()[0]
    total_users = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # Crucial: Fetch pending orders needing admin acceptance!
    pending_rows = cur.execute(
        "SELECT * FROM orders WHERE order_status = 'Pending' ORDER BY id DESC"
    ).fetchall()
    pending_orders = []
    for r in pending_rows:
        o = dict(r)
        items = cur.execute("SELECT * FROM order_items WHERE order_id = ?", (r["id"],)).fetchall()
        o["order_items"] = [dict(it) for it in items]
        pending_orders.append(o)

    # Recent orders
    recent_rows = cur.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 8").fetchall()
    recent_orders = [dict(r) for r in recent_rows]

    conn.close()

    return render_template(
        "admin/dashboard.html",
        total_revenue=total_revenue,
        total_orders=total_orders,
        pending_count=pending_count,
        total_plants=total_plants,
        total_users=total_users,
        pending_orders=pending_orders,
        recent_orders=recent_orders
    )


@app.route("/admin/plants")
@admin_required
def admin_plants():
    conn = get_db()
    cur = conn.cursor()
    plants = cur.execute("SELECT * FROM plants WHERE is_active = 1 ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/plants.html", plants=plants)


@app.route("/admin/add-plant", methods=["GET", "POST"])
@admin_required
def admin_add_plant():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        scientific_name = request.form.get("scientific_name", "").strip()
        category = request.form.get("category", "Flowering").strip()
        try:
            price = max(1.0, float(request.form.get("price", 249.0)))
        except (ValueError, TypeError):
            price = 249.0

        try:
            original_price = max(price, float(request.form.get("original_price", price + 100.0)))
        except (ValueError, TypeError):
            original_price = price + 100.0

        try:
            stock = max(0, int(request.form.get("stock", 20)))
        except (ValueError, TypeError):
            stock = 20
        sunlight = request.form.get("sunlight", "Bright Indirect Sunlight").strip()
        water = request.form.get("water", "2-3 times per week").strip()
        soil = request.form.get("soil", "Well-drained rich potting soil").strip()
        description = request.form.get("description", "").strip()
        featured = 1 if request.form.get("featured") == "1" else 0

        image_filename = "plantCareimage.jpeg"
        image_file = request.files.get("image")
        if image_file and allowed_file(image_file.filename):
            ext = secure_filename(image_file.filename).rsplit(".", 1)[1].lower()
            safe_name = f"{int(datetime.now().timestamp())}_{secure_filename(name.replace(' ', '_'))}.{ext}"
            image_file.save(os.path.join(UPLOAD_FOLDER, safe_name))
            image_filename = safe_name

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO plants (
                name, scientific_name, category, price, original_price, stock,
                sunlight, water, soil, description, image, featured, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            name, scientific_name, category, price, original_price, stock,
            sunlight, water, soil, description, image_filename, featured
        ))
        conn.commit()
        conn.close()

        flash(f"Plant '{name}' has been added to the catalog!", "success")
        return redirect(url_for("admin_plants"))

    return render_template("admin/add_plant.html")


@app.route("/admin/edit-plant/<int:id>", methods=["GET", "POST"])
@admin_required
def admin_edit_plant(id):
    conn = get_db()
    cur = conn.cursor()

    plant = cur.execute("SELECT * FROM plants WHERE id = ?", (id,)).fetchone()
    if not plant:
        conn.close()
        flash("Plant not found.", "error")
        return redirect(url_for("admin_plants"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        scientific_name = request.form.get("scientific_name", "").strip()
        category = request.form.get("category", "Flowering").strip()
        try:
            price = max(1.0, float(request.form.get("price", plant["price"])))
        except (ValueError, TypeError):
            price = float(plant["price"])

        try:
            original_price = max(price, float(request.form.get("original_price", plant["original_price"] or price)))
        except (ValueError, TypeError):
            original_price = float(plant["original_price"] or price)

        try:
            stock = max(0, int(request.form.get("stock", plant["stock"])))
        except (ValueError, TypeError):
            stock = int(plant["stock"])
        sunlight = request.form.get("sunlight", "").strip()
        water = request.form.get("water", "").strip()
        soil = request.form.get("soil", "").strip()
        description = request.form.get("description", "").strip()
        featured = 1 if request.form.get("featured") == "1" else 0

        image_filename = plant["image"]
        image_file = request.files.get("image")
        if image_file and allowed_file(image_file.filename):
            ext = secure_filename(image_file.filename).rsplit(".", 1)[1].lower()
            safe_name = f"{int(datetime.now().timestamp())}_{secure_filename(name.replace(' ', '_'))}.{ext}"
            image_file.save(os.path.join(UPLOAD_FOLDER, safe_name))
            image_filename = safe_name

        cur.execute("""
            UPDATE plants SET
                name = ?, scientific_name = ?, category = ?, price = ?, original_price = ?,
                stock = ?, sunlight = ?, water = ?, soil = ?, description = ?,
                image = ?, featured = ?
            WHERE id = ?
        """, (
            name, scientific_name, category, price, original_price,
            stock, sunlight, water, soil, description,
            image_filename, featured, id
        ))
        conn.commit()
        conn.close()

        flash(f"Updated '{name}' details successfully!", "success")
        return redirect(url_for("admin_plants"))

    conn.close()
    return render_template("admin/edit_plant.html", plant=plant)


@app.route("/admin/delete-plant/<int:id>", methods=["GET", "POST"])
@admin_required
def admin_delete_plant(id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM plants WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    flash("Plant removed from store catalog.", "info")
    return redirect(url_for("admin_plants"))


@app.route("/admin/orders")
@admin_required
def admin_orders():
    status_filter = request.args.get("status", "").strip()

    conn = get_db()
    cur = conn.cursor()

    counts = {
        "all": cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "pending": cur.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'Pending'").fetchone()[0],
        "accepted": cur.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'Accepted'").fetchone()[0],
        "shipped": cur.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'Shipped'").fetchone()[0],
        "delivered": cur.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'Delivered'").fetchone()[0],
        "rejected": cur.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'Rejected'").fetchone()[0],
    }

    if status_filter:
        order_rows = cur.execute(
            "SELECT * FROM orders WHERE order_status = ? ORDER BY id DESC",
            (status_filter,)
        ).fetchall()
    else:
        order_rows = cur.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()

    orders = []
    for r in order_rows:
        o = dict(r)
        items = cur.execute("SELECT * FROM order_items WHERE order_id = ?", (r["id"],)).fetchall()
        o["order_items"] = [dict(it) for it in items]
        orders.append(o)

    conn.close()

    return render_template(
        "admin/orders.html",
        orders=orders,
        current_status=status_filter,
        counts=counts
    )


# CRUCIAL: Admin Acceptance Route for Customer Purchase Requests
@app.route("/admin/order/<int:order_id>/accept", methods=["GET", "POST"])
@admin_required
def admin_accept_order(order_id):
    conn = get_db()
    cur = conn.cursor()

    order = cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "error")
        return redirect(url_for("admin_orders"))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE orders SET order_status = 'Accepted', updated_at = ? WHERE id = ?",
        (now_str, order_id)
    )
    conn.commit()
    conn.close()

    flash(f"Purchase request for {order['order_number']} has been ACCEPTED by Admin! 🌿", "success")
    return redirect(request.referrer or url_for("admin_orders"))


# Admin Rejection Route
@app.route("/admin/order/<int:order_id>/reject", methods=["GET", "POST"])
@admin_required
def admin_reject_order(order_id):
    conn = get_db()
    cur = conn.cursor()

    order = cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "error")
        return redirect(url_for("admin_orders"))

    # Restore inventory stock if rejected from Pending or Accepted
    if order["order_status"] in ("Pending", "Accepted"):
        items = cur.execute("SELECT plant_id, quantity FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
        for it in items:
            cur.execute("UPDATE plants SET stock = stock + ? WHERE id = ?", (it["quantity"], it["plant_id"]))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE orders SET order_status = 'Rejected', updated_at = ? WHERE id = ?",
        (now_str, order_id)
    )
    conn.commit()
    conn.close()

    flash(f"Order {order['order_number']} has been rejected.", "info")
    return redirect(request.referrer or url_for("admin_orders"))


# General Status update (Shipped, Delivered, etc.)
@app.route("/admin/order/<int:order_id>/update-status", methods=["POST"])
@admin_required
def admin_update_order_status(order_id):
    new_status = request.form.get("status", "").strip()
    if not new_status:
        return redirect(request.referrer or url_for("admin_orders"))

    conn = get_db()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE orders SET order_status = ?, updated_at = ? WHERE id = ?",
        (new_status, now_str, order_id)
    )
    conn.commit()
    conn.close()

    flash(f"Order status updated to '{new_status}'.", "success")
    return redirect(request.referrer or url_for("admin_orders"))


@app.route("/admin/users")
@admin_required
def admin_users():
    conn = get_db()
    cur = conn.cursor()

    users_rows = cur.execute("""
        SELECT u.*, COUNT(o.id) as order_count
        FROM users u
        LEFT JOIN orders o ON u.id = o.user_id
        GROUP BY u.id
        ORDER BY u.id DESC
    """).fetchall()

    users = [dict(u) for u in users_rows]
    conn.close()

    return render_template("admin/users.html", users=users)


def get_sales_report_data(selected_date="all"):
    """
    Fetches detailed purchase records: customer name, email, plant name,
    plant price, quantity, item total, daily total plants sold, and daily total revenue.
    """
    conn = get_db()
    cur = conn.cursor()

    # 1. Daily Aggregates (Day's total plants sold & Day's total revenue)
    daily_stats_query = """
        SELECT 
            SUBSTR(o.created_at, 1, 10) as order_date,
            SUM(oi.quantity) as plants_sold,
            SUM(oi.subtotal) as daily_revenue
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.order_status != 'Cancelled'
        GROUP BY SUBSTR(o.created_at, 1, 10)
    """
    daily_stats_rows = cur.execute(daily_stats_query).fetchall()
    daily_stats_map = {
        r["order_date"]: {
            "plants_sold": int(r["plants_sold"] or 0),
            "daily_revenue": float(r["daily_revenue"] or 0.0)
        }
        for r in daily_stats_rows
    }

    available_dates = sorted(list(daily_stats_map.keys()), reverse=True)

    # 2. Detailed Purchase Items
    query = """
        SELECT 
            SUBSTR(o.created_at, 1, 10) as order_date,
            o.created_at,
            o.order_number,
            o.customer_name,
            o.user_email,
            o.customer_phone,
            o.order_status,
            o.payment_method,
            oi.plant_name,
            oi.price as plant_price,
            oi.quantity,
            oi.subtotal as item_total
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.order_status != 'Cancelled'
    """
    params = []
    if selected_date and selected_date != "all":
        query += " AND SUBSTR(o.created_at, 1, 10) = ?"
        params.append(selected_date)

    query += " ORDER BY o.created_at DESC, o.id DESC"
    purchases_rows = cur.execute(query, params).fetchall()

    purchases = []
    total_qty_filtered = 0
    total_rev_filtered = 0.0

    for r in purchases_rows:
        item = dict(r)
        d = item["order_date"]
        # Attach day's aggregate stats to each purchase row
        day_stat = daily_stats_map.get(d, {"plants_sold": 0, "daily_revenue": 0.0})
        item["day_plants_sold"] = day_stat["plants_sold"]
        item["day_revenue"] = day_stat["daily_revenue"]
        purchases.append(item)
        total_qty_filtered += item["quantity"]
        total_rev_filtered += item["item_total"]

    conn.close()
    return purchases, daily_stats_map, available_dates, total_qty_filtered, total_rev_filtered


@app.route("/admin/sales")
@admin_required
def admin_sales():
    conn = get_db()
    cur = conn.cursor()

    total_sales_res = cur.execute(
        "SELECT SUM(total_amount) FROM orders WHERE order_status IN ('Accepted', 'Shipped', 'Delivered')"
    ).fetchone()[0]
    total_sales = total_sales_res if total_sales_res else 0.0

    completed_orders_count = cur.execute(
        "SELECT COUNT(*) FROM orders WHERE order_status IN ('Accepted', 'Shipped', 'Delivered')"
    ).fetchone()[0]

    pending_rev_res = cur.execute(
        "SELECT SUM(total_amount) FROM orders WHERE order_status = 'Pending'"
    ).fetchone()[0]
    pending_revenue = pending_rev_res if pending_rev_res else 0.0

    pending_orders_count = cur.execute(
        "SELECT COUNT(*) FROM orders WHERE order_status = 'Pending'"
    ).fetchone()[0]

    avg_order_value = (total_sales / completed_orders_count) if completed_orders_count > 0 else 0.0

    # Top selling plants
    top_plants_rows = cur.execute("""
        SELECT oi.plant_name, SUM(oi.quantity) as total_qty, SUM(oi.subtotal) as total_sales
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.order_status IN ('Accepted', 'Shipped', 'Delivered')
        GROUP BY oi.plant_name
        ORDER BY total_qty DESC
        LIMIT 6
    """).fetchall()
    top_plants = [dict(tp) for tp in top_plants_rows]

    # Order status distribution
    status_counts_rows = cur.execute(
        "SELECT order_status, COUNT(*) as cnt FROM orders GROUP BY order_status"
    ).fetchall()
    status_counts = {r["order_status"]: r["cnt"] for r in status_counts_rows}

    conn.close()

    # Detailed Customer Purchases Report & Daily Stats
    selected_date = request.args.get("date", "all").strip()
    purchases, daily_stats_map, available_dates, total_qty_filtered, total_rev_filtered = get_sales_report_data(selected_date)

    return render_template(
        "admin/sales.html",
        total_sales=total_sales,
        completed_orders_count=completed_orders_count,
        pending_revenue=pending_revenue,
        pending_orders_count=pending_orders_count,
        avg_order_value=avg_order_value,
        top_plants=top_plants,
        status_counts=status_counts,
        purchases=purchases,
        daily_stats_map=daily_stats_map,
        available_dates=available_dates,
        selected_date=selected_date,
        total_qty_filtered=total_qty_filtered,
        total_rev_filtered=total_rev_filtered
    )


@app.route("/admin/download-report")
@admin_required
def admin_download_report():
    """
    Generates and downloads a CSV Report of all customer plant purchases:
    Customer Name, Email, Phone, Plant Name, Unit Price, Quantity, Item Total,
    Day's Total Plants Sold, Day's Total Revenue, and Order Status.
    """
    selected_date = request.args.get("date", "all").strip()
    purchases, daily_stats_map, available_dates, total_qty, total_rev = get_sales_report_data(selected_date)

    output = io.StringIO()
    writer = csv.writer(output)

    # Report Header Info
    filter_label = f"Date: {selected_date}" if selected_date and selected_date != "all" else "All Dates"
    writer.writerow(["PlantCare Hub - Customer Purchases & Daily Sales Report"])
    writer.writerow([f"Generated On: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
    writer.writerow([f"Report Filter: {filter_label}"])
    writer.writerow([])

    # Table Columns
    writer.writerow([
        "Order Date",
        "Order Reference",
        "Customer Name",
        "Customer Email",
        "Customer Phone",
        "Plant Name",
        "Plant Unit Price (INR)",
        "Quantity Purchased",
        "Item Total (INR)",
        "Day's Total Plants Sold",
        "Day's Total Revenue (INR)",
        "Order Status",
        "Payment Method"
    ])

    for p in purchases:
        writer.writerow([
            p["order_date"],
            p["order_number"],
            p["customer_name"],
            p["user_email"],
            p["customer_phone"],
            p["plant_name"],
            f"{p['plant_price']:.2f}",
            p["quantity"],
            f"{p['item_total']:.2f}",
            p["day_plants_sold"],
            f"{p['day_revenue']:.2f}",
            p["order_status"],
            p.get("payment_method", "N/A")
        ])

    # Summary Row
    writer.writerow([])
    writer.writerow([
        "TOTAL SUMMARY",
        "",
        "",
        "",
        "",
        "",
        "",
        f"Total Plants Sold: {total_qty}",
        f"Total Revenue: INR {total_rev:.2f}",
        "",
        "",
        "",
        ""
    ])

    # Convert to UTF-8 with BOM for Excel compatibility on Windows
    csv_bytes = output.getvalue().encode("utf-8-sig")

    filename_date = selected_date if selected_date and selected_date != "all" else "all_dates"
    filename = f"plantcare_sales_report_{filename_date}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    response = make_response(csv_bytes)
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    response.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    return response


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)