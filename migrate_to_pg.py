import os
import sqlite3
import psycopg2
from dotenv import load_dotenv

load_dotenv()

pg_url = os.getenv("DATABASE_URL")
if not pg_url:
    print("DATABASE_URL is not set!")
    exit(1)

# Render fix if postgres:// is used
if pg_url.startswith("postgres://"):
    pg_url = pg_url.replace("postgres://", "postgresql://", 1)

print("Connecting to PostgreSQL...")
pg_conn = psycopg2.connect(pg_url)
pg_cur = pg_conn.cursor()

# Connect to SQLite database.db
sqlite_path = "database.db"
if not os.path.exists(sqlite_path):
    print("database.db not found!")
    exit(1)

sq_conn = sqlite3.connect(sqlite_path)
sq_conn.row_factory = sqlite3.Row
sq_cur = sq_conn.cursor()

print("Creating/updating tables on PostgreSQL to match app schema...")

# 1. Users Table
pg_cur.execute("""
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
);
""")
# Ensure missing columns exist in case table was created with old schema
pg_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin INTEGER DEFAULT 0;")
pg_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified INTEGER DEFAULT 1;")

# 2. Plants Table
pg_cur.execute("""
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
    owner_email TEXT DEFAULT '',
    watering_date TEXT DEFAULT '',
    fertilizer_date TEXT DEFAULT '',
    health_status TEXT DEFAULT 'Healthy',
    reminder_status TEXT DEFAULT 'Pending',
    fertilizer_status TEXT DEFAULT 'Pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")
pg_cur.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS is_active INTEGER DEFAULT 1;")
pg_cur.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS rating REAL DEFAULT 4.8;")
pg_cur.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS reviews_count INTEGER DEFAULT 14;")

# 3. Cart Table
# If old cart table had user_email instead of user_id, drop and recreate
pg_cur.execute("""
SELECT column_name FROM information_schema.columns 
WHERE table_name = 'cart' AND column_name = 'user_id'
""")
if not pg_cur.fetchone():
    print("Recreating cart table with user_id...")
    pg_cur.execute("DROP TABLE IF EXISTS cart CASCADE;")

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS cart (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    plant_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, plant_id)
);
""")

# 4. Wishlist Table
pg_cur.execute("""
SELECT column_name FROM information_schema.columns 
WHERE table_name = 'wishlist' AND column_name = 'user_id'
""")
if not pg_cur.fetchone():
    print("Recreating wishlist table with user_id...")
    pg_cur.execute("DROP TABLE IF EXISTS wishlist CASCADE;")

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS wishlist (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    plant_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, plant_id)
);
""")

# 5. Orders Table
pg_cur.execute("""
SELECT column_name FROM information_schema.columns 
WHERE table_name = 'orders' AND column_name = 'customer_name'
""")
if not pg_cur.fetchone():
    print("Recreating orders and order_items tables with current schema...")
    pg_cur.execute("DROP TABLE IF EXISTS order_items CASCADE;")
    pg_cur.execute("DROP TABLE IF EXISTS orders CASCADE;")

pg_cur.execute("""
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
);
""")

# 6. Order Items Table
pg_cur.execute("""
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
);
""")

pg_conn.commit()
print("PostgreSQL tables successfully created / aligned!")

# SYNC DATA FROM SQLITE TO POSTGRESQL

# Sync Users
sq_users = sq_cur.execute("SELECT * FROM users").fetchall()
for u in sq_users:
    u_dict = dict(u)
    # Check if user exists by email
    pg_cur.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(%s)", (u_dict["email"],))
    exists = pg_cur.fetchone()
    if not exists:
        pg_cur.execute("""
            INSERT INTO users (id, fullname, email, username, password, phone, address, city, pincode, is_admin, created_at, is_verified)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            u_dict["id"], u_dict["fullname"], u_dict["email"], u_dict.get("username", ""),
            u_dict["password"], u_dict.get("phone", ""), u_dict.get("address", ""),
            u_dict.get("city", ""), u_dict.get("pincode", ""), u_dict.get("is_admin", 0),
            u_dict.get("created_at", ""), u_dict.get("is_verified", 1)
        ))
    else:
        # Update existing user data
        pg_cur.execute("""
            UPDATE users SET fullname=%s, username=%s, password=%s, phone=%s, address=%s,
            city=%s, pincode=%s, is_admin=%s, created_at=%s, is_verified=%s
            WHERE LOWER(email) = LOWER(%s)
        """, (
            u_dict["fullname"], u_dict.get("username", ""), u_dict["password"],
            u_dict.get("phone", ""), u_dict.get("address", ""), u_dict.get("city", ""),
            u_dict.get("pincode", ""), u_dict.get("is_admin", 0), u_dict.get("created_at", ""),
            u_dict.get("is_verified", 1), u_dict["email"]
        ))
print(f"Synced {len(sq_users)} users.")

# Sync Plants
sq_plants = sq_cur.execute("SELECT * FROM plants").fetchall()
# Clean old plants if needed or upsert
for p in sq_plants:
    p_dict = dict(p)
    pg_cur.execute("SELECT id FROM plants WHERE id = %s", (p_dict["id"],))
    exists = pg_cur.fetchone()
    if not exists:
        pg_cur.execute("""
            INSERT INTO plants (id, name, scientific_name, category, price, original_price, stock,
                                water, sunlight, soil, description, image, featured, is_active,
                                rating, reviews_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            p_dict["id"], p_dict["name"], p_dict.get("scientific_name", ""), p_dict["category"],
            p_dict.get("price", 249.0), p_dict.get("original_price", 349.0), p_dict.get("stock", 20),
            p_dict.get("water", ""), p_dict.get("sunlight", ""), p_dict.get("soil", ""),
            p_dict.get("description", ""), p_dict.get("image", "plantCareimage.jpeg"),
            p_dict.get("featured", 0), p_dict.get("is_active", 1),
            p_dict.get("rating", 4.8), p_dict.get("reviews_count", 14)
        ))
    else:
        pg_cur.execute("""
            UPDATE plants SET name=%s, scientific_name=%s, category=%s, price=%s, original_price=%s,
                              stock=%s, water=%s, sunlight=%s, soil=%s, description=%s,
                              image=%s, featured=%s, is_active=%s, rating=%s, reviews_count=%s
            WHERE id = %s
        """, (
            p_dict["name"], p_dict.get("scientific_name", ""), p_dict["category"],
            p_dict.get("price", 249.0), p_dict.get("original_price", 349.0), p_dict.get("stock", 20),
            p_dict.get("water", ""), p_dict.get("sunlight", ""), p_dict.get("soil", ""),
            p_dict.get("description", ""), p_dict.get("image", "plantCareimage.jpeg"),
            p_dict.get("featured", 0), p_dict.get("is_active", 1),
            p_dict.get("rating", 4.8), p_dict.get("reviews_count", 14),
            p_dict["id"]
        ))
print(f"Synced {len(sq_plants)} plants.")

# Sync Orders
sq_orders = sq_cur.execute("SELECT * FROM orders").fetchall()
for o in sq_orders:
    o_dict = dict(o)
    pg_cur.execute("SELECT id FROM orders WHERE id = %s", (o_dict["id"],))
    exists = pg_cur.fetchone()
    if not exists:
        pg_cur.execute("""
            INSERT INTO orders (id, order_number, user_id, user_email, customer_name, customer_phone,
                               shipping_address, city, pincode, payment_method, payment_status,
                               subtotal, shipping_fee, total_amount, order_status, admin_notes,
                               created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            o_dict["id"], o_dict["order_number"], o_dict.get("user_id"), o_dict["user_email"],
            o_dict["customer_name"], o_dict["customer_phone"], o_dict["shipping_address"],
            o_dict["city"], o_dict["pincode"], o_dict.get("payment_method", "Cash on Delivery"),
            o_dict.get("payment_status", "Pending"), o_dict.get("subtotal", 0.0),
            o_dict.get("shipping_fee", 0.0), o_dict.get("total_amount", 0.0),
            o_dict.get("order_status", "Pending"), o_dict.get("admin_notes", ""),
            o_dict.get("created_at"), o_dict.get("updated_at")
        ))
print(f"Synced {len(sq_orders)} orders.")

# Sync Order Items
sq_order_items = sq_cur.execute("SELECT * FROM order_items").fetchall()
for oi in sq_order_items:
    oi_dict = dict(oi)
    pg_cur.execute("SELECT id FROM order_items WHERE id = %s", (oi_dict["id"],))
    exists = pg_cur.fetchone()
    if not exists:
        pg_cur.execute("""
            INSERT INTO order_items (id, order_id, plant_id, plant_name, plant_image, price, quantity, subtotal)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            oi_dict["id"], oi_dict["order_id"], oi_dict["plant_id"], oi_dict["plant_name"],
            oi_dict.get("plant_image", ""), oi_dict["price"], oi_dict["quantity"], oi_dict["subtotal"]
        ))
print(f"Synced {len(sq_order_items)} order items.")

# Reset serial sequences to max(id)
for tbl in ['users', 'plants', 'orders', 'order_items', 'cart', 'wishlist']:
    pg_cur.execute(f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), coalesce((SELECT MAX(id) FROM {tbl}), 1));")

pg_conn.commit()
print("PostgreSQL data synchronization and sequence reset complete!")

pg_conn.close()
sq_conn.close()
