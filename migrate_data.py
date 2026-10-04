import os
import sqlite3
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# Old local SQLite database
sqlite_conn = sqlite3.connect("database.db")
sqlite_conn.row_factory = sqlite3.Row
sqlite_cursor = sqlite_conn.cursor()

# Render PostgreSQL
pg_conn = psycopg2.connect(os.getenv("DATABASE_URL_EXTERNAL"))
pg_cursor = pg_conn.cursor()
pg_cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    fullname TEXT,
    email TEXT UNIQUE,
    username TEXT,
    password TEXT
)
""")

pg_cursor.execute("""
CREATE TABLE IF NOT EXISTS plants (
    id SERIAL PRIMARY KEY,
    name TEXT,
    scientific_name TEXT,
    water TEXT,
    sunlight TEXT,
    soil TEXT,
    image TEXT,
    owner_email TEXT,
    category TEXT,
    watering_date TEXT,
    fertilizer_date TEXT,
    health_status TEXT DEFAULT 'Healthy',
    reminder_status TEXT DEFAULT 'upcoming',
    fertilizer_status TEXT DEFAULT 'upcoming'
)
""")

pg_conn.commit()

# ---------------- USERS ----------------

sqlite_cursor.execute("SELECT * FROM users")
users = sqlite_cursor.fetchall()

user_columns = [
    "fullname",
    "email",
    "username",
    "password"
]

for user in users:
    pg_cursor.execute("""
        INSERT INTO users (fullname, email, username, password)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (
        user["fullname"],
        user["email"],
        user["username"],
        user["password"]
    ))

# ---------------- PLANTS ----------------

sqlite_cursor.execute("SELECT * FROM plants")
plants = sqlite_cursor.fetchall()

plant_columns = [
    "name",
    "scientific_name",
    "water",
    "sunlight",
    "soil",
    "image",
    "owner_email",
    "category",
    "watering_date",
    "fertilizer_date",
    "health_status",
    "reminder_status",
    "fertilizer_status"
]

for plant in plants:
    pg_cursor.execute("""
        INSERT INTO plants (
            name,
            scientific_name,
            water,
            sunlight,
            soil,
            image,
            owner_email,
            category,
            watering_date,
            fertilizer_date,
            health_status,
            reminder_status,
            fertilizer_status
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
    """, (
        plant["name"],
        plant["scientific_name"],
        plant["water"],
        plant["sunlight"],
        plant["soil"],
        plant["image"],
        plant["owner_email"],
        plant["category"],
        plant["watering_date"],
        plant["fertilizer_date"],
        plant["health_status"],
        plant["reminder_status"],
        plant["fertilizer_status"]
    ))

pg_conn.commit()

sqlite_conn.close()
pg_conn.close()

print("Migration completed successfully!")
print("Users migrated:", len(users))
print("Plants migrated:", len(plants))