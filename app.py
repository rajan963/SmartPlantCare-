from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file



import sqlite3
import os
import smtplib
import secrets
import hashlib
import requests

from io import BytesIO
from flask import send_file
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors


from werkzeug.utils import secure_filename
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from dotenv import load_dotenv
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

print("OpenRouter key loaded:", bool(OPENROUTER_API_KEY))
print(
    "OpenRouter key length:",
    len(OPENROUTER_API_KEY) if OPENROUTER_API_KEY else 0
)


def create_database():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS plants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    try:
        cursor.execute(
            "ALTER TABLE plants ADD COLUMN owner_email TEXT"
        )
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fullname TEXT,
        email TEXT UNIQUE,
        username TEXT,
        password TEXT
    )
    """)

    conn.commit()
    conn.close()


app = Flask(__name__)

@app.route("/")
def home():
    return render_template("index.html")

app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Admin Email
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

def is_admin():

    if "user" not in session:
        return False

    return session["user"].lower() == ADMIN_EMAIL.lower()

def admin_required():

    if "user" not in session:
        return redirect("/login")

    if not is_admin():
        return redirect("/dashboard")

    return None

# ================= GMAIL OTP CONFIGURATION =================

GMAIL_SENDER = os.getenv("GMAIL_SENDER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

OTP_EXPIRY_MINUTES = 5


def send_otp_email(receiver_email, otp):

    message = EmailMessage()

    message["Subject"] = "PlantCare Hub - Your Login OTP"
    message["From"] = GMAIL_SENDER
    message["To"] = receiver_email

    message.set_content(f"""
Hello,

Your PlantCare Hub login OTP is:

{otp}

This OTP is valid for 5 minutes.

If you did not request this OTP, please ignore this email.

Regards,
PlantCare Hub
Smart Agriculture System
""")

    with smtplib.SMTP("smtp.gmail.com", 587) as server:

        server.starttls()

        server.login(
            GMAIL_SENDER,
            GMAIL_APP_PASSWORD.replace(" ", "")
        )

        server.send_message(message)

@app.route("/test-ai")
def test_ai():
    return render_template("test_ai.html")

@app.route("/ai")
def ai():
    if "user" not in session:
        return redirect("/login")

    return render_template("ai_chat.html")


@app.route("/plant-ai", methods=["POST"])
def plant_ai():

    try:

        question = request.form.get("question", "").strip()
        image = request.files.get("image")

        if not question and not image:
            return jsonify({
                "success": False,
                "answer": "Please ask a plant-related question or upload a plant photo."
            }), 400

        if not OPENROUTER_API_KEY:
            return jsonify({
                "success": False,
                "answer": "OpenRouter API key is missing. Check your .env file."
            }), 500

        prompt = """
You are PlantCare AI 🌱.

Answer ONLY questions related to plants and gardening.

You can help with:

- Plant identification
- Scientific name
- Plant type
- Watering
- Sunlight
- Soil
- Fertilizer
- NPK
- Growth
- Flowering
- Propagation
- Pruning
- Pests
- Diseases
- Plant health
- Indoor plants
- Outdoor plants
- Vegetables
- Fruits
- Flowers
- Trees
- Medicinal plants

If an image is provided:

Identify the plant if possible and provide:

1. Plant name
2. Scientific name
3. Plant type
4. Sunlight requirements
5. Water requirements
6. Soil requirements
7. Fertilizer
8. Growth information
9. Flowering information
10. Propagation
11. Common pests
12. Common diseases
13. Complete care instructions

Never claim certainty if the image is unclear.

Reply in the user's language:

English → English
Hindi → Hindi
Hinglish → Hinglish

If the question is unrelated to plants, say:

"I am PlantCare AI 🌱. I can only help with plants and gardening."
"""

        user_text = question

        if not user_text:
            user_text = (
                "Analyze this plant image and provide complete "
                "plant identification and care information."
            )

        content = [
            {
                "type": "text",
                "text": prompt + "\n\nUser: " + user_text
            }
        ]

        # ================= IMAGE =================

        if image and image.filename:

            allowed_types = [
                "image/jpeg",
                "image/png",
                "image/webp"
            ]

            if image.mimetype not in allowed_types:

                return jsonify({
                    "success": False,
                    "answer": "Please upload JPG, PNG or WEBP."
                }), 400

            image_data = image.read()

            if len(image_data) > 10 * 1024 * 1024:

                return jsonify({
                    "success": False,
                    "answer": "Image must be under 10 MB."
                }), 400

            import base64

            base64_image = base64.b64encode(
                image_data
            ).decode("utf-8")

            image_url = (
                f"data:{image.mimetype};base64,"
                f"{base64_image}"
            )

            content.append({
                "type": "image_url",
                "image_url": {
                    "url": image_url
                }
            })

        # ================= OPENROUTER =================

        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",

            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://127.0.0.1:5000",
                "X-Title": "PlantCare Hub"
            },

            json={
                "model": "openrouter/free",

                "messages": [
                    {
                        "role": "user",
                        "content": content
                    }
                ]
            },

            timeout=120
        )

        print("OPENROUTER STATUS:", response.status_code)

        data = response.json()

        print("OPENROUTER RESPONSE:", data)

        if response.status_code != 200:

            error_message = data.get(
                "error",
                data
            )

            return jsonify({
                "success": False,
                "answer": f"OpenRouter AI Error: {error_message}"
            }), response.status_code

        if "choices" not in data or not data["choices"]:

            return jsonify({
                "success": False,
                "answer": "OpenRouter returned no AI response."
            }), 500

        answer = data["choices"][0]["message"]["content"]

        return jsonify({
            "success": True,
            "answer": answer
        })

    except requests.exceptions.Timeout:

        return jsonify({
            "success": False,
            "answer": "OpenRouter request timed out. Please try again."
        }), 504

    except requests.exceptions.RequestException as e:

        print("OPENROUTER REQUEST ERROR:", repr(e))

        return jsonify({
            "success": False,
            "answer": f"OpenRouter connection error: {e}"
        }), 500

    except Exception as e:

        print("PLANT AI ERROR:", repr(e))

        return jsonify({
            "success": False,
            "answer": f"AI Error: {e}"
        }), 500



def generate_otp():

    return f"{secrets.randbelow(1000000):06d}"


def hash_otp(otp):

    return hashlib.sha256(
        otp.encode()
    ).hexdigest()


UPLOAD_FOLDER = "static/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER





@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        action = request.form.get(
            "action",
            "send_otp"
        )

        # ================= VERIFY OTP =================

        if action == "verify_otp":

            entered_otp = request.form.get(
                "otp",
                ""
            ).strip()

            saved_hash = session.get("otp_hash")
            otp_email = session.get("otp_email")
            otp_expiry = session.get("otp_expiry")

            if not saved_hash or not otp_email or not otp_expiry:

                return render_template(
                    "login.html",
                    error="OTP session expired. Please login again.",
                    otp_mode=False
                )

            try:

                expiry_time = datetime.fromisoformat(
                    otp_expiry
                )

            except ValueError:

                session.pop("otp_hash", None)
                session.pop("otp_email", None)
                session.pop("otp_expiry", None)

                return render_template(
                    "login.html",
                    error="OTP expired. Please login again.",
                    otp_mode=False
                )

            if datetime.now() > expiry_time:

                session.pop("otp_hash", None)
                session.pop("otp_email", None)
                session.pop("otp_expiry", None)

                return render_template(
                    "login.html",
                    error="OTP has expired. Please request a new OTP.",
                    otp_mode=False
                )

            if hash_otp(entered_otp) != saved_hash:

                return render_template(
                    "login.html",
                    error="Invalid OTP. Please try again.",
                    otp_mode=True,
                    otp_email=otp_email
                )

            # OTP correct

            session["user"] = otp_email

            session.pop("otp_hash", None)
            session.pop("otp_email", None)
            session.pop("otp_expiry", None)

            return redirect("/dashboard")


        # ================= SEND OTP =================

        email = request.form.get(
            "email",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (email, password)
        )

        user = cursor.fetchone()

        conn.close()

        if not user:

            return render_template(
                "login.html",
                error="Invalid Email or Password",
                otp_mode=False
            )

        # Generate OTP

        otp = generate_otp()

        try:

            send_otp_email(
                email,
                otp
            )

        except Exception as e:

            print(
                "OTP EMAIL ERROR:",
                e
            )

            return render_template(
                "login.html",
                error="OTP could not be sent. Please check Gmail configuration.",
                otp_mode=False
            )

        # Store OTP securely as hash

        session["otp_hash"] = hash_otp(otp)

        session["otp_email"] = email

        session["otp_expiry"] = (
            datetime.now()
            + timedelta(
                minutes=OTP_EXPIRY_MINUTES
            )
        ).isoformat()

        return render_template(
            "login.html",
            otp_mode=True,
            otp_email=email,
            message="OTP sent successfully to your email."
        )

    return render_template(
        "login.html",
        otp_mode=False
    )


    # =========================================================
# FORGOT PASSWORD OTP
# =========================================================

def send_forgot_password_otp_email(receiver_email, otp):
    msg = EmailMessage()

    msg["Subject"] = "PlantCare Hub - Password Reset OTP"
    msg["From"] = GMAIL_SENDER
    msg["To"] = receiver_email

    msg.set_content(f"""
Hello,

Your PlantCare Hub password reset OTP is:

{otp}

This OTP is valid for {OTP_EXPIRY_MINUTES} minutes.

If you did not request a password reset, please ignore this email.

PlantCare Hub
""")

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()

        server.login(
            GMAIL_SENDER,
            GMAIL_APP_PASSWORD.replace(" ", "")
        )

        server.send_message(msg)


# =========================================================
# FORGOT PASSWORD
# =========================================================

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip()

        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id FROM users WHERE email=?",
            (email,)
        )

        user = cursor.fetchone()

        conn.close()

        if not user:

            return render_template(
                "login.html",
                forgot_mode=True,
                error="No account found with this Gmail address."
            )

        # Generate OTP

        otp = generate_otp()

        try:

            send_forgot_password_otp_email(
                email,
                otp
            )

        except Exception as e:

            print(
                "FORGOT PASSWORD OTP ERROR:",
                e
            )

            return render_template(
                "login.html",
                forgot_mode=True,
                error="OTP could not be sent. Please check Gmail configuration."
            )

        # Store forgot-password OTP separately

        session["forgot_otp_hash"] = hash_otp(otp)

        session["forgot_otp_email"] = email

        session["forgot_otp_expiry"] = (
            datetime.now()
            + timedelta(
                minutes=OTP_EXPIRY_MINUTES
            )
        ).isoformat()

        return render_template(
            "login.html",
            forgot_otp_mode=True,
            otp_email=email,
            message="Password reset OTP sent successfully."
        )

    return render_template(
        "login.html",
        forgot_mode=True
    )


# =========================================================
# VERIFY FORGOT PASSWORD OTP
# =========================================================

@app.route("/forgot-password/verify", methods=["POST"])
def verify_forgot_password():

    entered_otp = request.form.get(
        "otp",
        ""
    ).strip()

    saved_hash = session.get(
        "forgot_otp_hash"
    )

    email = session.get(
        "forgot_otp_email"
    )

    expiry = session.get(
        "forgot_otp_expiry"
    )

    if not saved_hash or not email or not expiry:

        return render_template(
            "login.html",
            forgot_otp_mode=True,
            error="OTP session expired. Please request a new OTP.",
            otp_email=email
        )

    try:

        expiry_time = datetime.fromisoformat(
            expiry
        )

    except ValueError:

        session.pop("forgot_otp_hash", None)
        session.pop("forgot_otp_email", None)
        session.pop("forgot_otp_expiry", None)

        return render_template(
            "login.html",
            forgot_mode=True,
            error="OTP expired. Please request a new OTP."
        )

    if datetime.now() > expiry_time:

        session.pop("forgot_otp_hash", None)
        session.pop("forgot_otp_email", None)
        session.pop("forgot_otp_expiry", None)

        return render_template(
            "login.html",
            forgot_mode=True,
            error="OTP has expired. Please request a new OTP."
        )

    if hash_otp(entered_otp) != saved_hash:

        return render_template(
            "login.html",
            forgot_otp_mode=True,
            error="Invalid OTP. Please try again.",
            otp_email=email
        )

    # OTP correct

    session["forgot_verified"] = True

    return render_template(
        "login.html",
        reset_password_mode=True,
        otp_email=email
    )


# =========================================================
# RESET PASSWORD
# =========================================================

@app.route("/forgot-password/reset", methods=["POST"])
def reset_password():

    if not session.get("forgot_verified"):

        return redirect("/forgot-password")

    email = session.get(
        "forgot_otp_email"
    )

    new_password = request.form.get(
        "new_password",
        ""
    )

    confirm_password = request.form.get(
        "confirm_password",
        ""
    )

    if not email:

        return redirect("/forgot-password")

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
            error="Passwords do not match."
        )

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users
        SET password=?
        WHERE email=?
        """,
        (
            new_password,
            email
        )
    )

    conn.commit()
    conn.close()

    # Clear forgot-password session

    session.pop("forgot_otp_hash", None)
    session.pop("forgot_otp_email", None)
    session.pop("forgot_otp_expiry", None)
    session.pop("forgot_verified", None)

    return render_template(
        "login.html",
        reset_success=True,
        message="Password changed successfully. Please login with your new password."
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        fullname = request.form["fullname"]
        email = request.form["email"]
        username = request.form["username"]
        password = request.form["password"]

        conn = sqlite3.connect("database.db")
        cur = conn.cursor()

        # Check duplicate email/username
        cur.execute(
    "SELECT * FROM users WHERE email=?",
    (email,))

        if cur.fetchone():
            conn.close()
            return render_template(
                "register.html",
                error="Email already exists!"
            )

        cur.execute(
            "INSERT INTO users (fullname,email,username,password) VALUES (?,?,?,?)",
            (fullname, email, username, password)
        )

        conn.commit()
        conn.close()

        return redirect("/login")

    return render_template("register.html")


@app.route("/dashboard")
def dashboard():

    if "user" not in session:
        return redirect("/login")

    email = session["user"]
    today = date.today().isoformat()

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    # ================= TOTAL PLANTS =================
    cursor.execute("""
        SELECT COUNT(*)
        FROM plants
        WHERE owner_email=?
    """, (email,))
    total_plants = cursor.fetchone()[0]

    # ================= NEED WATER =================
    cursor.execute("""
        SELECT COUNT(*)
        FROM plants
        WHERE owner_email=?
        AND watering_date < ?
        AND reminder_status != 'Completed'
    """, (email, today))
    need_water = cursor.fetchone()[0]

    # ================= NEED FERTILIZER =================
    cursor.execute("""
        SELECT COUNT(*)
        FROM plants
        WHERE owner_email=?
        AND fertilizer_date < ?
        AND fertilizer_status != 'Completed'
    """, (email, today))
    need_fertilizer = cursor.fetchone()[0]

    # ================= HEALTHY PLANTS =================
    # Healthy = Watering due nahi hai
    # AND Fertilizer due nahi hai
    cursor.execute("""
        SELECT COUNT(*)
        FROM plants
        WHERE owner_email=?
        AND (
            watering_date >= ?
            OR reminder_status = 'Completed'
        )
        AND (
            fertilizer_date >= ?
            OR fertilizer_status = 'Completed'
        )
    """, (email, today, today))

    healthy = cursor.fetchone()[0]

    # ================= RECENT PLANTS =================
    cursor.execute("""
        SELECT *
        FROM plants
        WHERE owner_email=?
        ORDER BY id DESC
        LIMIT 5
    """, (email,))

    plants = cursor.fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        ADMIN_EMAIL=ADMIN_EMAIL,
        total_plants=total_plants,
        healthy=healthy,
        need_water=need_water,
        need_fertilizer=need_fertilizer,
        plants=plants
    )

@app.route("/addplant", methods=["GET", "POST"])
def addplant():

    if "user" not in session:
        return redirect("/login")

    if request.method == "POST":

        image = request.files["image"]
        filename = secure_filename(image.filename)

        image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()

        cursor.execute("""
        INSERT INTO plants
    (
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            request.form["name"],
            request.form["scientific_name"],
            request.form["water"],
            request.form["sunlight"],
            request.form["soil"],
            filename,
            session["user"],
            request.form["category"],
            request.form["watering_date"],
            request.form["fertilizer_date"],
            "Healthy",
            "upcoming",
            "upcoming"
        ))

        conn.commit()
        conn.close()

        return redirect("/plantlist")

    return render_template("addplant.html")

@app.route("/plantlist")
def plantlist():

    if "user" not in session:
        return redirect("/login")

    search = request.args.get("search", "")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    if search:
        cursor.execute(
            """
            SELECT * FROM plants
            WHERE owner_email=? AND name LIKE ?
            ORDER BY id DESC
            """,
            (session["user"], '%' + search + '%')
        )
    else:
        cursor.execute(
            """
            SELECT * FROM plants
            WHERE owner_email=?
            ORDER BY id DESC
            """,
            (session["user"],)
        )

    plants = cursor.fetchall()

    conn.close()

    return render_template(
        "plantlist.html",
        plants=plants
    )

@app.route("/delete/<int:id>")
def delete(id):

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM plants WHERE id=? AND owner_email=?",
        (id, session["user"])
    )

    conn.commit()
    conn.close()

    return redirect("/plantlist")

@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit(id):

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    # Check that this plant belongs to the logged-in user
    cursor.execute(
        "SELECT * FROM plants WHERE id=? AND owner_email=?",
        (id, session["user"])
    )

    plant = cursor.fetchone()

    if not plant:
        conn.close()
        return redirect("/plantlist")

    if request.method == "POST":

        cursor.execute("""
        UPDATE plants
        SET
            name=?,
            scientific_name=?,
            water=?,
            sunlight=?,
            soil=?,
            category=?,
            watering_date=?,
            fertilizer_date=?,
            health_status=?,
            reminder_status='Pending',
            fertilizer_status='Pending'
        WHERE id=? AND owner_email=?
        """, (

            request.form["name"],
            request.form["scientific_name"],
            request.form["water"],
            request.form["sunlight"],
            request.form["soil"],
            request.form["category"],
            request.form["watering_date"],
            request.form["fertilizer_date"],
            request.form["health_status"],
            id,
            session["user"]

        ))

        conn.commit()
        conn.close()

        return redirect("/plantlist")

    conn.close()

    return render_template(
        "editplant.html",
        plant=plant
    )

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")



@app.route("/reminders")
def reminders():

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM plants WHERE owner_email=?",
        (session["user"],)
    )

    plants = cursor.fetchall()

    reminder_data = []
    today = date.today()

    for plant in plants:

        watering_date = date.fromisoformat(plant["watering_date"])

        if plant["reminder_status"] == "Completed":
            status = "Completed"
        elif watering_date < today:
            status = "Overdue"
        elif watering_date == today:
            status = "Today"
        else:
            status = "Pending"

        reminder_data.append({
            "id": plant["id"],
            "plant": plant["name"],
            "watering_date": watering_date.strftime("%d-%m-%Y"),
            "status": status
        })

    conn.close()

    return render_template(
        "reminders.html",
        reminders=reminder_data
    )


@app.route("/fertilizer")
def fertilizer():

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM plants WHERE owner_email=?",
        (session["user"],)
    )

    plants = cursor.fetchall()

    fertilizer_data = []
    today = date.today()

    for plant in plants:

        fertilizer_date = date.fromisoformat(
            plant["fertilizer_date"]
        )

        if plant["fertilizer_status"] == "Completed":
            status = "Completed"
        elif fertilizer_date < today:
            status = "Overdue"
        elif fertilizer_date == today:
            status = "Today"
        else:
            status = "upcoming"

        fertilizer_data.append({
            "id": plant["id"],
            "plant": plant["name"],
            "fertilizer_date": fertilizer_date.strftime("%d-%m-%Y"),
            "status": status
        })

    conn.close()

    return render_template(
        "fertilizer.html",
        fertilizers=fertilizer_data
    )


@app.route("/health")
def health():

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM plants WHERE owner_email=?",
        (session["user"],)
    )

    plants = cursor.fetchall()

    today = date.today()
    health = []

    for plant in plants:

        watering_date = date.fromisoformat(
            plant["watering_date"]
        )

        fertilizer_date = date.fromisoformat(
            plant["fertilizer_date"]
        )

        water_overdue = (
            watering_date < today
            and plant["reminder_status"] != "Completed"
        )

        fertilizer_overdue = (
            fertilizer_date < today
            and plant["fertilizer_status"] != "Completed"
        )

        if water_overdue and fertilizer_overdue:
            status = "Sick"

        elif water_overdue or fertilizer_overdue:
            status = "Needs Attention"

        else:
            status = "Healthy"

        cursor.execute("""
            UPDATE plants
            SET health_status=?
            WHERE id=? AND owner_email=?
        """, (
            status,
            plant["id"],
            session["user"]
        ))

        health.append({
            "plant": plant["name"],
            "status": status
        })

    conn.commit()
    conn.close()

    return render_template(
        "health.html",
        health=health
    )


@app.route("/categories")
def categories():

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT category, COUNT(*) as total
        FROM plants
        WHERE owner_email=?
        GROUP BY category
    """, (
        session["user"],
    ))

    categories = cursor.fetchall()

    conn.close()

    return render_template(
        "categories.html",
        categories=categories
    )


@app.route("/complete_reminder/<int:id>")
def complete_reminder(id):

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE plants
        SET reminder_status='Completed'
        WHERE id=? AND owner_email=?
    """, (
        id,
        session["user"]
    ))

    conn.commit()
    conn.close()

    return redirect("/reminders")


@app.route("/complete_fertilizer/<int:id>")
def complete_fertilizer(id):

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE plants
        SET fertilizer_status='Completed'
        WHERE id=? AND owner_email=?
    """, (
        id,
        session["user"]
    ))

    conn.commit()
    conn.close()

    return redirect("/fertilizer")


@app.route("/category/<category>")
def category_plants(category):

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT * FROM plants
        WHERE category=? AND owner_email=?
        """,
        (
            category,
            session["user"]
        )
    )

    plants = cursor.fetchall()

    conn.close()

    return render_template(
        "category_plants.html",
        plants=plants,
        category=category
    )

@app.route("/profile")
def profile():

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM users WHERE email=?",
        (session["user"],)
    )

    user = cursor.fetchone()

    conn.close()

    return render_template("profile.html", user=user)

@app.route("/edit_profile", methods=["GET", "POST"])
def edit_profile():

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if request.method == "POST":

        fullname = request.form["fullname"]
        username = request.form["username"]
        password = request.form["password"]

        cursor.execute("""
        UPDATE users
        SET fullname=?, username=?, password=?
        WHERE email=?
        """, (
            fullname,
            username,
            password,
            session["user"]
        ))

        conn.commit()
        conn.close()

        return redirect("/profile")

    cursor.execute(
        "SELECT * FROM users WHERE email=?",
        (session["user"],)
    )

    user = cursor.fetchone()

    conn.close()

    return render_template("edit_profile.html", user=user)



def create_pdf(plants,title):
    pdf=BytesIO()
    doc=SimpleDocTemplate(pdf,pagesize=(595,842),rightMargin=35,leftMargin=35,topMargin=35,bottomMargin=35)
    s=getSampleStyleSheet()
    today=date.today().isoformat()

    def st(d,x):
        if x=="Completed": return x
        if not d: return "Pending"
        return "Overdue" if d<today else "Today" if d==today else "Upcoming"

    fields=[
        ("Plant Name","name"),("Scientific Name","scientific_name"),
        ("Category","category"),("Water Requirement","water"),
        ("Sunlight","sunlight"),("Soil","soil"),
        ("Watering Date","watering_date"),("Watering Status","reminder_status"),
        ("Fertilizer Date","fertilizer_date"),("Fertilizer Status","fertilizer_status"),
        ("Health Status","health_status")]

    content=[Paragraph("<b>PLANTCARE HUB</b>",s["Title"]),
             Paragraph(title,s["Heading2"]),
             Paragraph(f"<b>Total Plants: {len(plants)}</b>",s["Normal"]),Spacer(1,18)]

    for p in plants:
        data=[]
        for label,key in fields:
            v=p[key]
            if key=="reminder_status": v=st(p["watering_date"],v)
            if key=="fertilizer_status": v=st(p["fertilizer_date"],v)
            data.append([Paragraph(f"<b>{label}</b>",s["Normal"]),Paragraph(str(v),s["Normal"])])
        t=Table(data,colWidths=[180,340])
        t.setStyle(TableStyle([
            ("GRID",(0,0),(-1,-1),.5,colors.lightgrey),
            ("BACKGROUND",(0,0),(0,-1),colors.whitesmoke),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ("LEFTPADDING",(0,0),(-1,-1),8),
            ("TOPPADDING",(0,0),(-1,-1),7),
            ("BOTTOMPADDING",(0,0),(-1,-1),7)]))
        content += [t,Spacer(1,18)]

    doc.build(content)
    pdf.seek(0)
    return pdf


@app.route("/download-report/<report_type>")
def download_report(report_type):
    if "user" not in session:
        return redirect("/login")

    if report_type not in ["all", "water", "fertilizer"]:
        return redirect("/report")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    if report_type == "all":
        title = "All Plants Report"
        filename = "PlantCare_Hub_All_Plants_Report.pdf"
        plants = conn.execute("""
            SELECT * FROM plants
            WHERE owner_email=?
            ORDER BY id DESC
        """, (session["user"],)).fetchall()

    elif report_type == "water":
        title = "Need Water Report"
        filename = "PlantCare_Hub_Need_Water_Report.pdf"
        plants = conn.execute("""
            SELECT * FROM plants
            WHERE owner_email=?
            AND watering_date < ?
            AND reminder_status != 'Completed'
            ORDER BY watering_date
        """, (session["user"], today)).fetchall()

    else:
        title = "Need Fertilizer Report"
        filename = "PlantCare_Hub_Need_Fertilizer_Report.pdf"
        plants = conn.execute("""
            SELECT * FROM plants
            WHERE owner_email=?
            AND fertilizer_date < ?
            AND fertilizer_status != 'Completed'
            ORDER BY fertilizer_date
        """, (session["user"], today)).fetchall()

    conn.close()

    pdf = create_pdf(plants, title)

    return send_file(
        pdf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf"
    )

@app.route("/report-view/<report_type>")
def report_view(report_type):
    if "user" not in session:
        return redirect("/login")

    if report_type not in ["all", "water", "fertilizer"]:
        return redirect("/report")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    if report_type == "all":
        title = "🌿 All Plants Report"
        plants = conn.execute("""
            SELECT * FROM plants
            WHERE owner_email=?
            ORDER BY id DESC
        """, (session["user"],)).fetchall()

    elif report_type == "water":
        title = "💧 Need Water Report"
        plants = conn.execute("""
            SELECT * FROM plants
            WHERE owner_email=?
            AND watering_date < ?
            AND reminder_status != 'Completed'
            ORDER BY watering_date
        """, (session["user"], today)).fetchall()

    else:
        title = "🌱 Need Fertilizer Report"
        plants = conn.execute("""
            SELECT * FROM plants
            WHERE owner_email=?
            AND fertilizer_date < ?
            AND fertilizer_status != 'Completed'
            ORDER BY fertilizer_date
        """, (session["user"], today)).fetchall()

    conn.close()

    return render_template(
        "report_view.html",
        plants=plants,
        title=title,
        report_type=report_type
    )




@app.route("/report")
def report():

    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM plants
        WHERE owner_email=?
        ORDER BY id DESC
    """, (session["user"],))

    plants = cursor.fetchall()

    conn.close()

    return render_template(
        "report.html",
        plants=plants
    )


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/admin")
def admin():

    access = admin_required()

    if access:
        return access

    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row

    # Users
    users = conn.execute("""
        SELECT id, fullname, email, username
        FROM users
        ORDER BY id DESC
    """).fetchall()

    # Plants
    plants = conn.execute("""
        SELECT id, name, scientific_name, water, sunlight, soil
        FROM plants
        ORDER BY id DESC
    """).fetchall()

    # Counts
    total_users = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    total_plants = conn.execute(
        "SELECT COUNT(*) FROM plants"
    ).fetchone()[0]

    conn.close()

    return render_template(
        "admin.html",
        users=users,
        plants=plants,
        total_users=total_users,
        total_plants=total_plants
    )



create_database()

if __name__ == "__main__":
    app.run(debug=True)