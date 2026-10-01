from flask import Flask, request, render_template, redirect, url_for, session, jsonify
import sqlite3
import secrets
import string
import os
import requests
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


app = Flask(__name__)
app.secret_key = "tripsync_secret_key"
DATABASE = "C:/Users/Public/tripsync.db"
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------------- DATABASE ----------------
def get_db_connection():
    conn = sqlite3.connect(DATABASE) 
    conn.row_factory = sqlite3.Row 
    return conn

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def create_table():
    conn = get_db()
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_name TEXT NOT NULL,
            trip_code TEXT UNIQUE NOT NULL,
            creator_id INTEGER NOT NULL,
            FOREIGN KEY (creator_id) REFERENCES users(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trip_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(trip_id, user_id),
            FOREIGN KEY (trip_id) REFERENCES trips(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            payer_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trip_id) REFERENCES trips(id),
            FOREIGN KEY (payer_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            uploader_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trip_id) REFERENCES trips(id),
            FOREIGN KEY (uploader_id) REFERENCES users(id)
        )
    """)
    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS crowd_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_name TEXT NOT NULL,
            crowd_level INTEGER NOT NULL,
            report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


# ---------------- HOME ----------------
@app.route("/")
def home():
    return render_template("index.html")


# ---------------- REGISTER ----------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    name = request.form.get("name")
    email = request.form.get("email")
    password = request.form.get("password")

    if not name or not email or not password:
        return "All fields are required!"

    hashed_password = generate_password_hash(password)

    try:
        conn = get_db()
        conn.execute("""
            INSERT INTO users (name, email, password)
            VALUES (?, ?, ?)
        """, (name, email, hashed_password))
        conn.commit()
        conn.close()
        return redirect(url_for("login"))
    except sqlite3.IntegrityError:
        return "Email already registered!"


# ---------------- LOGIN (WITH DEBUGGING) ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email")
    password = request.form.get("password")

    print(f"DEBUG -> Login attempt: Email='{email}'")

    conn = get_db()
    user = conn.execute("""
        SELECT * FROM users
        WHERE email = ?
    """, (email,)).fetchone()
    conn.close()

    if not user:
        print("DEBUG -> Error: Email not found in database!")
        return "Error: This Email is NOT registered! Please go back and Register."

    if check_password_hash(user["password"], password):
        print("DEBUG -> Success: Password matched!")
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        return redirect(url_for("dashboard"))
    else:
        print("DEBUG -> Error: Password did not match!")
        return "Error: Wrong Password! Please go back and try again."


# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    trips = conn.execute("""
        SELECT trips.*
        FROM trips
        JOIN trip_members
        ON trips.id = trip_members.trip_id
        WHERE trip_members.user_id = ?
    """, (session["user_id"],)).fetchall()
    conn.close()

    return render_template(
        "dashboard.html",
        name=session["user_name"],
        trips=trips
    )


# ---------------- CREATE TRIP ----------------
@app.route("/create-trip", methods=["POST"])
def create_trip():
    if "user_id" not in session:
        return redirect(url_for("login"))

    trip_name = request.form.get("trip_name")
    if not trip_name:
        return "Trip name is required!"

    characters = string.ascii_uppercase + string.digits
    while True:
        trip_code = "".join(secrets.choice(characters) for _ in range(6))
        conn = get_db()
        existing = conn.execute("SELECT id FROM trips WHERE trip_code = ?", (trip_code,)).fetchone()
        if not existing:
            break
        conn.close()

    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO trips (trip_name, trip_code, creator_id)
        VALUES (?, ?, ?)
    """, (trip_name, trip_code, session["user_id"]))
    
    trip_id = cursor.lastrowid

    conn.execute("""
        INSERT INTO trip_members (trip_id, user_id)
        VALUES (?, ?)
    """, (trip_id, session["user_id"]))

    conn.commit()
    conn.close()

    return redirect(url_for("dashboard"))


# ---------------- JOIN TRIP ----------------
@app.route("/join-trip", methods=["POST"])
def join_trip():
    if "user_id" not in session:
        return redirect(url_for("login"))

    trip_code = request.form.get("trip_code")
    if not trip_code:
        return "Trip code is required!"

    trip_code = trip_code.upper()
    conn = get_db()
    trip = conn.execute("SELECT * FROM trips WHERE trip_code = ?", (trip_code,)).fetchone()

    if not trip:
        conn.close()
        return "Invalid Trip Code!"

    try:
        conn.execute("""
            INSERT INTO trip_members (trip_id, user_id)
            VALUES (?, ?)
        """, (trip["id"], session["user_id"]))
        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))
    except sqlite3.IntegrityError:
        conn.close()
        return "You are already a member of this trip!"


# ---------------- VIEW TRIP DETAILS ----------------
@app.route("/trip/<int:trip_id>")
def view_trip(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    is_member = conn.execute("""
        SELECT * FROM trip_members
        WHERE trip_id = ? AND user_id = ?
    """, (trip_id, session["user_id"])).fetchone()

    if not is_member:
        conn.close()
        return "Access Denied: You are not a member of this trip!"

    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    
    members = conn.execute("""
        SELECT users.name, users.email 
        FROM users
        JOIN trip_members ON users.id = trip_members.user_id
        WHERE trip_members.trip_id = ?
    """, (trip_id,)).fetchall()

    expenses = conn.execute("""
        SELECT expenses.amount, expenses.description, expenses.date, users.name as payer_name
        FROM expenses
        JOIN users ON expenses.payer_id = users.id
        WHERE expenses.trip_id = ?
        ORDER BY expenses.date DESC
    """, (trip_id,)).fetchall()

    total_expense = conn.execute("""
        SELECT SUM(amount) as total FROM expenses WHERE trip_id = ?
    """, (trip_id,)).fetchone()["total"]

    photos = conn.execute("""
        SELECT photos.filename, photos.uploader_id, users.name as uploader_name
        FROM photos
        JOIN users ON photos.uploader_id = users.id
        WHERE photos.trip_id = ?
        ORDER BY photos.upload_date DESC
    """, (trip_id,)).fetchall()

    conn.close()

    return render_template(
        "trip_detail.html", 
        trip=trip, 
        members=members, 
        expenses=expenses, 
        total_expense=total_expense or 0,
        photos=photos
    )


# ---------------- ADD EXPENSE ----------------
@app.route("/add-expense/<int:trip_id>", methods=["POST"])
def add_expense(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    amount = request.form.get("amount")
    description = request.form.get("description")

    if not amount or not description:
        return "Amount and description are required!"

    conn = get_db()
    conn.execute("""
        INSERT INTO expenses (trip_id, payer_id, amount, description)
        VALUES (?, ?, ?, ?)
    """, (trip_id, session["user_id"], float(amount), description))
    
    conn.commit()
    conn.close()

    return redirect(url_for("view_trip", trip_id=trip_id))


# ---------------- LEAVE TRIP ----------------
@app.route("/leave-trip/<int:trip_id>", methods=["POST"])
def leave_trip(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    conn = get_db()
    conn.execute("""
        DELETE FROM trip_members
        WHERE trip_id = ? AND user_id = ?
    """, (trip_id, session["user_id"]))
    
    conn.commit()
    conn.close()
    
    return redirect(url_for("dashboard"))


# ---------------- UPLOAD PHOTO ----------------
@app.route("/upload-photo/<int:trip_id>", methods=["POST"])
def upload_photo(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    if 'photo' not in request.files:
        return "No file uploaded!"

    file = request.files['photo']
    if file.filename == '':
        return "No selected file"

    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        conn = get_db()
        conn.execute("""
            INSERT INTO photos (trip_id, uploader_id, filename)
            VALUES (?, ?, ?)
        """, (trip_id, session["user_id"], filename))
        conn.commit()
        conn.close()

    return redirect(url_for("view_trip", trip_id=trip_id))


# ---------------- DELETE PHOTO ----------------
@app.route("/delete-photo/<int:trip_id>/<photo_filename>", methods=["POST"])
def delete_photo(trip_id, photo_filename):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    photo = conn.execute("""
        SELECT * FROM photos 
        WHERE filename = ? AND uploader_id = ? AND trip_id = ?
    """, (photo_filename, session["user_id"], trip_id)).fetchone()

    if photo:
        conn.execute("""
            DELETE FROM photos WHERE id = ?
        """, (photo["id"],))
        conn.commit()
        
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo_filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            
    conn.close()
    return redirect(url_for("view_trip", trip_id=trip_id))


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.route('/api/chat', methods=['POST'])
def chat_api():
    global chat_session 
    
    try:
        data = request.get_json()
        user_message = data.get("user_message")
        trip_id = data.get("trip_id")  

        if not user_message:
            return jsonify({"error": "Message required"}), 400

        # 1. Database se is trip ka data nikalna
        conn = get_db()
        expenses = conn.execute('''
            SELECT e.description, e.amount, u.name as payer
            FROM expenses e
            JOIN users u ON e.payer_id = u.id
            WHERE e.trip_id = ?
        ''', (trip_id,)).fetchall()
        conn.close()

        
        expense_text = "Is trip ke saare kharche (Expenses):\n"
        if expenses:
            for exp in expenses:
                expense_text += f"- {exp['payer']} ne ₹{exp['amount']} diye '{exp['description']}' ke liye.\n"
        else:
            expense_text += "Abhi tak koi kharcha nahi hua hai.\n"

        
        ai_prompt = f"""
        You are a smart financial data analyst for this trip. 
        Here is the real-time expense data from the database:
        {expense_text}
        
        User's Question: {user_message}
        
        Instruction: Answer the user's question accurately based ONLY on the expense data provided above. 
        Calculate totals or find who paid what if asked. Keep it short, friendly, and in Hinglish or English.
        """

    
        response = chat_session.send_message(ai_prompt)
        
        return jsonify({"bot_reply": response.text})
        
    except Exception as e:
        print("❌ CHATBOT ERROR: ", str(e))
        return jsonify({"error": str(e)}), 500


# ---------------- CROWD & ANALYTICS APIs ----------------
@app.route('/api/submit-crowd', methods=['POST'])
def submit_crowd():
    data = request.json
    location = data.get('location').lower()
    level = data.get('level') 
    
    conn = get_db_connection()
    conn.execute('INSERT INTO crowd_reports (location_name, crowd_level) VALUES (?, ?)', (location, level))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "message": "Crowd report verified and saved!"})


@app.route('/analytics/<int:trip_id>')
def analytics(trip_id):
    return render_template('analytics.html', trip_id=trip_id)


@app.route('/api/real-analytics-data/<int:trip_id>')
def real_analytics_data(trip_id):
    conn = get_db()
    try:
        # Category Wise
        cat_data = conn.execute('''
            SELECT description, SUM(amount) as total 
            FROM expenses 
            WHERE trip_id = ?
            GROUP BY description
        ''', (trip_id,)).fetchall()
        
        try:
            member_data = conn.execute('''
                SELECT u.name as real_name, SUM(e.amount) as total 
                FROM expenses e
                JOIN users u ON e.payer_id = u.id
                WHERE e.trip_id = ?
                GROUP BY e.payer_id
            ''', (trip_id,)).fetchall()
            names_list = [row['real_name'] for row in member_data]
        except:
            member_data = conn.execute('SELECT payer_id, SUM(amount) as total FROM expenses WHERE trip_id = ? GROUP BY payer_id', (trip_id,)).fetchall()
            names_list = [f"User {row['payer_id']}" for row in member_data]
            
        amounts_list = [row['total'] for row in member_data]

        # Area Chart Trend
        trend_data = conn.execute('''
            SELECT description, amount 
            FROM expenses 
            WHERE trip_id = ?
            ORDER BY id ASC
        ''', (trip_id,)).fetchall()

        conn.close()

        return jsonify({
            "status": "success",
            "categories": {
                "labels": [row['description'] for row in cat_data],
                "values": [row['total'] for row in cat_data]
            },
            "members": {
                "names": names_list,
                "amounts": amounts_list
            },
            "daily": {
                "dates": [row['description'] for row in trend_data],
                "totals": [row['amount'] for row in trend_data]
            }
        })
    except Exception as e:
        if conn:
            conn.close()
        return jsonify({"status": "error", "message": str(e)})


@app.route('/api/get-crowd/<location_name>', methods=['GET'])
def get_crowd(location_name):
    location = location_name.lower()
    current_hour = datetime.now().hour
    
    #  Historical Time Logic (30% Weight)
    time_score = 30 
    if 10 <= current_hour <= 17:
        time_score = 85 
    elif 17 < current_hour <= 21:
        time_score = 95 
    elif 7 <= current_hour < 10:
        time_score = 50 
        
    #  REAL Live Weather API Logic (30% Weight)
    weather_multiplier = 1.0 
    weather_condition = "Clear"
    try:
        url = f"https://wttr.in/{location}?format=j1"
        res = requests.get(url, timeout=3).json()
        current_condition = res['current_condition'][0]['weatherDesc'][0]['value']
        
        if any(word in current_condition for word in ['Rain', 'Shower', 'Drizzle', 'Thunder']):
            weather_multiplier = 0.4  
            weather_condition = "Raining 🌧️"
        elif any(word in current_condition for word in ['Cloud', 'Overcast', 'Fog', 'Mist']):
            weather_multiplier = 0.9  
            weather_condition = "Cloudy ☁️"
        elif any(word in current_condition for word in ['Clear', 'Sunny']):
            weather_multiplier = 1.1  
            weather_condition = "Clear & Sunny ☀️"
        else:
            weather_condition = current_condition
    except Exception as e:
        weather_condition = "Clear (Default) 🌤️"
        
    # FACTOR 3: Real User Reports (40% Weight)
    conn = get_db_connection()
    recent_reports = conn.execute(
        "SELECT crowd_level FROM crowd_reports WHERE location_name = ? ORDER BY report_time DESC LIMIT 5",
        (location,)
    ).fetchall()
    conn.close()
    
    user_score = 0
    if recent_reports:
        total = sum(row['crowd_level'] for row in recent_reports)
        user_score = total / len(recent_reports)
    else:
        user_score = time_score 

    # FINAL HYBRID CALCULATION (Weighted Average)
    base_calc = (time_score * 0.3) + (user_score * 0.7)
    final_crowd_percentage = int(base_calc * weather_multiplier)
    final_crowd_percentage = max(10, min(100, final_crowd_percentage))

    return jsonify({
        "location": location_name.title(),
        "crowd_percentage": final_crowd_percentage,
        "weather": weather_condition,
        "reports_analyzed": len(recent_reports)
    })

def setup_database():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS crowd_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_name TEXT NOT NULL,
            crowd_level INTEGER NOT NULL,
            report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


# ---------------- START SERVER ----------------
if __name__ == "__main__":
    setup_database()
    create_table()
    app.run(debug=True)