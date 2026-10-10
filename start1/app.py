from flask import Flask, request, render_template, redirect, url_for, session, jsonify
import sqlite3
import secrets
import string
import os
import requests
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session, flash, get_flashed_messages

app = Flask(__name__)
app.secret_key = "tripsync_secret_key"

# Single unified database path to prevent mismatch issues
DATABASE = "C:/Users/Public/tripsync.db"
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Global chat session placeholder if used with external AI SDK
chat_session = None 


import os
import json
import sqlite3
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from duckduckgo_search import DDGS

app = Flask(__name__)
app.secret_key = "bharatyatra_super_secret_agentic_key"
DATABASE = 'bharatyatra.db'

# ---------------- DATABASE SETUP ----------------
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Users Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL
        )
    """)
    
    # Trips Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT NOT NULL,
            destination TEXT NOT NULL,
            budget REAL DEFAULT 0.0
        )
    """)
    
    # Expenses Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER,
            payer_id INTEGER,
            amount REAL NOT NULL,
            description TEXT NOT NULL
        )
    """)
    
    # History Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            description TEXT,
            amount TEXT
        )
    """)
    
    # Itineraries Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS itineraries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER,
            day_number INTEGER,
            title TEXT,
            activities TEXT
        )
    """)
    
    conn.commit()
    conn.close()

init_db()


# ---------------- LIVE FX CONVERSION ENGINE ----------------
FX_CACHE = {}

def get_exchange_rate(from_curr, to_curr="INR"):
    if from_curr == to_curr:
        return 1.0
    cache_key = f"{from_curr}_{to_curr}"
    if cache_key in FX_CACHE:
        return FX_CACHE[cache_key]
        
    try:
        url = f"https://open.er-api.com/v6/latest/{from_curr}"
        res = requests.get(url, timeout=3).json()
        if res.get("result") == "success":
            rate = res["rates"].get(to_curr, 1.0)
            FX_CACHE[cache_key] = rate
            return rate
    except Exception as e:
        print(f"FX API Error: {e}")
    
    fallback = {"USD": 83.5, "EUR": 90.2, "AED": 22.7, "GBP": 105.0, "CAD": 61.5, "INR": 1.0}
    return round(fallback.get(from_curr, 1.0) / fallback.get(to_curr, 1.0), 4)


# ---------------- AGENT LOCAL TOOLS ----------------
def tool_add_expense(trip_id, user_id, description, amount, currency="INR"):
    """Tool 1: Converts foreign amount and inserts expense into DB."""
    rate = get_exchange_rate(currency, "INR")
    converted_inr = round(float(amount) * rate, 2)
    
    full_desc = f"{description} ({amount} {currency})" if currency != "INR" else description
    
    conn = get_db()
    conn.execute(
        "INSERT INTO expenses (trip_id, payer_id, amount, description) VALUES (?, ?, ?, ?)",
        (trip_id, user_id, converted_inr, full_desc)
    )
    conn.execute(
        "INSERT INTO history (user_id, title, description, amount) VALUES (?, ?, ?, ?)",
        (user_id, "AI Agent Expense Logged", full_desc, f"₹{converted_inr}")
    )
    conn.commit()
    conn.close()
    
    return {
        "status": "success",
        "added_amount_inr": converted_inr,
        "message": f"Expense '{description}' of {amount} {currency} (₹{converted_inr}) logged into database."
    }

def tool_get_trip_summary(trip_id):
    """Tool 2: Calculates total spent and fetches recent transactions."""
    conn = get_db()
    total_row = conn.execute("SELECT SUM(amount) as total FROM expenses WHERE trip_id = ?", (trip_id,)).fetchone()
    recent = conn.execute("SELECT description, amount FROM expenses WHERE trip_id = ? ORDER BY id DESC LIMIT 5", (trip_id,)).fetchall()
    conn.close()
    
    total = total_row["total"] if total_row and total_row["total"] else 0.0
    items = [f"{r['description']}: ₹{r['amount']}" for r in recent]
    
    return {
        "total_spent_inr": total,
        "recent_expenses": items
    }

def tool_create_roadmap(trip_id, user_id, days_plan):
    """Tool 3: Saves day-wise itinerary into DB."""
    conn = get_db()
    conn.execute("DELETE FROM itineraries WHERE trip_id = ?", (trip_id,))
    
    for plan in days_plan:
        conn.execute("""
            INSERT INTO itineraries (trip_id, day_number, title, activities)
            VALUES (?, ?, ?, ?)
        """, (trip_id, plan.get('day', 1), plan.get('title', ''), plan.get('activities', '')))
    
    conn.execute("""
        INSERT INTO history (user_id, title, description, amount)
        VALUES (?, ?, ?, ?)
    """, (user_id, "AI Travel Roadmap Created", f"{len(days_plan)}-Day Itinerary Plan Saved", "Roadmap"))
    
    conn.commit()
    conn.close()
    
    return {
        "status": "success",
        "message": f"Successfully created and saved {len(days_plan)}-day travel roadmap in database!"
    }

def tool_web_search(query):
    """Tool 4: Fetches real-time factual info from the web."""
    try:
        results = DDGS().text(keywords=query, max_results=3)
        if not results:
            return {"status": "error", "message": "No live results found."}
        
        snippets = [f"Source: {r['title']}\nDetails: {r['body']}" for r in results]
        return {
            "status": "success",
            "query": query,
            "web_data": "\n---\n".join(snippets)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------- GEMINI TOOL SCHEMAS ----------------
GEMINI_TOOLS = [{
    "function_declarations": [
        {
            "name": "add_expense",
            "description": "Logs an expense into database in any currency with automatic INR conversion.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "description": {"type": "STRING", "description": "Item description e.g. Dinner, Taxi, Flight"},
                    "amount": {"type": "NUMBER", "description": "Numeric amount spent"},
                    "currency": {"type": "STRING", "description": "3-letter currency code e.g. USD, EUR, AED, INR"}
                },
                "required": ["description", "amount"]
            }
        },
        {
            "name": "get_trip_summary",
            "description": "Returns total expense sum and recent log breakdown for active trip.",
            "parameters": {"type": "OBJECT", "properties": {}}
        },
        {
            "name": "create_travel_roadmap",
            "description": "Generates and saves full multi-day travel itinerary into database.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "days_plan": {
                        "type": "ARRAY",
                        "description": "List of daily itinerary objects",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "day": {"type": "INTEGER", "description": "Day index e.g. 1, 2, 3"},
                                "title": {"type": "STRING", "description": "Day theme"},
                                "activities": {"type": "STRING", "description": "Detailed morning, afternoon, evening plan"}
                            },
                            "required": ["day", "title", "activities"]
                        }
                    }
                },
                "required": ["days_plan"]
            }
        },
        {
            "name": "web_search",
            "description": "Performs live web search for weather, opening hours, local rules, or travel facts.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {"type": "STRING", "description": "Search query e.g. 'Paris metro ticket price 2026'"}
                },
                "required": ["query"]
            }
        }
    ]
}]


# ---------------- LIVE TRIP CONTEXT INJECTOR ----------------
def get_live_trip_context(trip_id):
    if not trip_id:
        return "No specific trip active."
        
    conn = get_db()
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        conn.close()
        return "Trip not found."

    spent_row = conn.execute("SELECT SUM(amount) as total FROM expenses WHERE trip_id = ?", (trip_id,)).fetchone()
    total_spent = spent_row['total'] if spent_row and spent_row['total'] else 0.0
    
    recent_exp = conn.execute("SELECT description, amount FROM expenses WHERE trip_id = ? ORDER BY id DESC LIMIT 5", (trip_id,)).fetchall()
    exp_str = ", ".join([f"{r['description']} (₹{r['amount']})" for r in recent_exp]) if recent_exp else "None"

    itinerary = conn.execute("SELECT day_number, title FROM itineraries WHERE trip_id = ? ORDER BY day_number ASC", (trip_id,)).fetchall()
    itin_str = ", ".join([f"Day {r['day_number']}: {r['title']}" for r in itinerary]) if itinerary else "None"

    conn.close()

    return f"""
    [ACTIVE TRIP DB CONTEXT]
    - Trip Title: {trip['title']}
    - Destination: {trip['destination']}
    - Total Budget: ₹{trip['budget']}
    - Total Logged Expenses: ₹{total_spent}
    - Recent Expenses: {exp_str}
    - Active Itinerary: {itin_str}
    """


# ---------------- AGENTIC AI ENDPOINT ----------------
@app.route('/api/agent', methods=['POST'])
def agentic_ai():
    if "user_id" not in session:
        return jsonify({"reply": "Please login to access AI Agent."}), 401

    data = request.get_json() or {}
    user_prompt = data.get("prompt", "").strip()
    trip_id = data.get("trip_id")

    if not user_prompt or not trip_id:
        return jsonify({"reply": "User prompt and valid Trip ID are required!"})

    db_context = get_live_trip_context(trip_id)

    system_instruction = f"""
    You are 'Bharatyatra Agentic AI', an autonomous, real-time international travel assistant.
    
    OPERATIONAL RULES:
    1. Answer queries accurately using [ACTIVE TRIP DB CONTEXT].
    2. Execute 'add_expense' when asked to log purchases or expenses.
    3. Execute 'create_travel_roadmap' when asked to build or save day-wise trip itineraries.
    4. Execute 'web_search' when asked for live weather, museum opening hours, flight status, or real-world facts.
    5. Always be concise, polite, clear, and direct.

    {db_context}
    """

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"{system_instruction}\n\nUser Request: {user_prompt}"}]}
        ],
        "tools": GEMINI_TOOLS
    }

    try:
        res = requests.post(url, json=payload, timeout=12).json()
        
        if 'candidates' not in res or not res['candidates']:
            return jsonify({"reply": "API Response error. Verify your GEMINI_API_KEY."})

        candidate = res['candidates'][0]['content']['parts'][0]

        # Check if Gemini triggered a Tool
        if "functionCall" in candidate:
            call = candidate["functionCall"]
            fn_name = call["name"]
            args = call.get("args", {})

            # Local Tool Execution
            tool_result = {}
            if fn_name == "add_expense":
                tool_result = tool_add_expense(
                    trip_id=trip_id,
                    user_id=session["user_id"],
                    description=args.get("description", "Expense"),
                    amount=args.get("amount", 0),
                    currency=args.get("currency", "INR")
                )
            elif fn_name == "get_trip_summary":
                tool_result = tool_get_trip_summary(trip_id)
            elif fn_name == "create_travel_roadmap":
                tool_result = tool_create_roadmap(
                    trip_id=trip_id,
                    user_id=session["user_id"],
                    days_plan=args.get("days_plan", [])
                )
            elif fn_name == "web_search":
                tool_result = tool_web_search(query=args.get("query", ""))

            # Pass Tool Execution Result back to Gemini
            second_payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": f"{system_instruction}\n\nUser Request: {user_prompt}"}]},
                    {"role": "model", "parts": [{"functionCall": call}]},
                    {
                        "role": "function",
                        "parts": [{
                            "functionResponse": {
                                "name": fn_name,
                                "response": tool_result
                            }
                        }]
                    }
                ],
                "tools": GEMINI_TOOLS
            }

            second_res = requests.post(url, json=second_payload, timeout=12).json()
            final_reply = second_res['candidates'][0]['content']['parts'][0]['text']
            return jsonify({"reply": final_reply, "tool_executed": fn_name})

        else:
            return jsonify({"reply": candidate['text']})

    except Exception as e:
        print(f"Agent Execution Failure: {e}")
        return jsonify({"reply": "Agent failed to respond due to network/API error."})


# ---------------- REGULAR ROUTES ----------------
@app.route('/')
def index():
    # Demo Auto-Login
    session["user_id"] = 1
    conn = get_db()
    
    # Ensure Default User & Trip Exist
    user = conn.execute("SELECT * FROM users WHERE id=1").fetchone()
    if not user:
        conn.execute("INSERT INTO users (id, name, email) VALUES (1, 'Sharma', 'sharma@example.com')")
        
    trip = conn.execute("SELECT * FROM trips WHERE id=1").fetchone()
    if not trip:
        conn.execute("INSERT INTO trips (id, user_id, title, destination, budget) VALUES (1, 1, 'Euro Trip 2026', 'Paris, France', 150000.0)")
        
    conn.commit()
    conn.close()
    return redirect(url_for('view_trip', trip_id=1))

@app.route('/trip/<int:trip_id>')
def view_trip(trip_id):
    conn = get_db()
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    expenses = conn.execute("SELECT * FROM expenses WHERE trip_id = ? ORDER BY id DESC", (trip_id,)).fetchall()
    itineraries = conn.execute("SELECT * FROM itineraries WHERE trip_id = ? ORDER BY day_number ASC", (trip_id,)).fetchall()
    spent_row = conn.execute("SELECT SUM(amount) as total FROM expenses WHERE trip_id = ?", (trip_id,)).fetchone()
    conn.close()
    
    total_spent = spent_row['total'] if spent_row and spent_row['total'] else 0.0
    return render_template('trip_detail.html', trip=trip, expenses=expenses, itineraries=itineraries, total_spent=total_spent)

if __name__ == '__main__':
    app.run(debug=True, port=5000)

# ---------------- DATABASE HELPERS ----------------
def get_db_connection():
    conn = sqlite3.connect(DATABASE) 
    conn.row_factory = sqlite3.Row 
    return conn

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def create_tables():
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            amount TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()



FX_CACHE = {}

def get_exchange_rate(from_curr, to_curr="INR"):
    if from_curr == to_curr:
        return 1.0
    cache_key = f"{from_curr}_{to_curr}"
    if cache_key in FX_CACHE:
        return FX_CACHE[cache_key]
    try:
        url = f"https://open.er-api.com/v6/latest/{from_curr}"
        res = requests.get(url, timeout=3).json()
        if res.get("result") == "success":
            rate = res["rates"].get(to_curr, 1.0)
            FX_CACHE[cache_key] = rate
            return rate
    except Exception as e:
        print(f"FX API Error: {e}")
    
    fallback = {"USD": 83.5, "EUR": 90.2, "AED": 22.7, "GBP": 105.0, "CAD": 61.5, "INR": 1.0}
    return round(fallback.get(from_curr, 1.0) / fallback.get(to_curr, 1.0), 4)


# ---------------- HOME & AUTH ----------------
@app.route("/")
def home():
    return render_template("index.html")

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

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email")
    password = request.form.get("password")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not user:
        return "Error: This Email is NOT registered!"

    if check_password_hash(user["password"], password):
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        return redirect(url_for("dashboard"))
    else:
        return "Error: Wrong Password!"

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------- DASHBOARD & TRIPS ----------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    trips = conn.execute("""
        SELECT trips.*
        FROM trips
        JOIN trip_members ON trips.id = trip_members.trip_id
        WHERE trip_members.user_id = ?
    """, (session["user_id"],)).fetchall()
    conn.close()

    return render_template("dashboard.html", name=session["user_name"], trips=trips)

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
        conn.close()
        if not existing:
            break

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

    # Add activity in history
    conn.execute("""
        INSERT INTO history (user_id, title, description, amount)
        VALUES (?, ?, ?, ?)
    """, (session["user_id"], "Trip Created", f"Created trip: {trip_name}", "Free"))

    conn.commit()
    conn.close()

    return redirect(url_for("dashboard"))

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
        
        conn.execute("""
            INSERT INTO history (user_id, title, description, amount)
            VALUES (?, ?, ?, ?)
        """, (session["user_id"], "Joined Trip", f"Joined trip ID: {trip['id']}", "Free"))

        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))
    except sqlite3.IntegrityError:
        conn.close()
        return "You are already a member of this trip!"

@app.route("/trip/<int:trip_id>")
def view_trip(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    is_member = conn.execute("""
        SELECT * FROM trip_members WHERE trip_id = ? AND user_id = ?
    """, (trip_id, session["user_id"])).fetchone()

    if not is_member:
        conn.close()
        return "Access Denied: You are not a member of this trip!"

    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    
    members = conn.execute("""
        SELECT users.id, users.name, users.email 
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


# ---------------- EXPENSES & HISTORY ----------------
@app.route("/add-expense/<int:trip_id>", methods=["POST"])
def add_expense(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    amount_raw = request.form.get("amount")
    description = request.form.get("description")
    currency = request.form.get("currency", "INR") # Naya Multi-Currency Dropdown field

    if not amount_raw or not description:
        return "Amount and description are required!"

    # 1. Real-time FX Conversion into INR
    raw_float = float(amount_raw)
    rate = get_exchange_rate(currency, "INR")
    converted_inr = round(raw_float * rate, 2)

    # 2. Description Format (Agar currency INR na ho)
    if currency != "INR":
        full_description = f"{description} ({raw_float} {currency})"
        history_amount = f"₹{converted_inr} ({raw_float} {currency})"
    else:
        full_description = description
        history_amount = f"₹{converted_inr}"

    conn = get_db()
    
    # 3. Insert into expenses (INR Amount & Description)
    conn.execute("""
        INSERT INTO expenses (trip_id, payer_id, amount, description)
        VALUES (?, ?, ?, ?)
    """, (trip_id, session["user_id"], converted_inr, full_description))
    
    # 4. Insert into history table
    conn.execute("""
        INSERT INTO history (user_id, title, description, amount)
        VALUES (?, ?, ?, ?)
    """, (session["user_id"], "Expense Added", full_description, history_amount))

    conn.commit()
    conn.close()

    return redirect(url_for("view_trip", trip_id=trip_id))

@app.route('/history')
def history():
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    user_id = session.get('user_id')  
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM history WHERE user_id = ? ORDER BY timestamp DESC", (user_id,))
    user_history = cursor.fetchall()
    conn.close()
    
    return render_template('history.html', history_items=user_history)


# ---------------- REAL-TIME USABLE ICON PAGES (BALANCE, PAY, WEALTH) ----------------
# ---------------- BALANCE & SETTLEMENTS ----------------
@app.route('/balance')
def balance():
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    user_id = session["user_id"]
    conn = get_db()
    
    # 1. User ki saari trips fetch karein jisme wo member hai
    user_trips = conn.execute("""
        SELECT trips.id, trips.trip_name 
        FROM trips
        JOIN trip_members ON trips.id = trip_members.trip_id
        WHERE trip_members.user_id = ?
    """, (user_id,)).fetchall()
    
    trip_balances = []
    total_overall_paid = 0
    
    for trip in user_trips:
        trip_id = trip['id']
        
        # Is trip ka total kharcha
        trip_total = conn.execute("""
            SELECT SUM(amount) as total FROM expenses WHERE trip_id = ?
        """, (trip_id,)).fetchone()["total"] or 0
        
        # Is trip ke total members
        member_count = conn.execute("""
            SELECT COUNT(*) as cnt FROM trip_members WHERE trip_id = ?
        """, (trip_id,)).fetchone()["cnt"] or 1
        
        # User ne is trip me kitna pay kiya
        user_paid = conn.execute("""
            SELECT SUM(amount) as total FROM expenses WHERE trip_id = ? AND payer_id = ?
        """, (trip_id, user_id)).fetchone()["total"] or 0
        
        total_overall_paid += user_paid
        
        # Fair share per person (Equal split logic)
        fair_share = trip_total / member_count if member_count > 0 else 0
        balance_amount = user_paid - fair_share
        
        trip_balances.append({
            "trip_name": trip["trip_name"],
            "trip_id": trip_id,
            "trip_total": trip_total,
            "user_paid": user_paid,
            "fair_share": fair_share,
            "balance_amount": balance_amount # Positive matlab lena hai, Negative matlab Dena hai
        })
        
    conn.close()
    
    return render_template('balance.html', trip_balances=trip_balances, total_overall_paid=total_overall_paid)

# ---------------- PAY & SETTLEMENT ROUTE ----------------
@app.route('/pay', methods=['GET', 'POST'])
def pay():
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    user_id = session["user_id"]
    conn = get_db()
    
    if request.method == 'POST':
        upi_id = request.form.get('upi_id')
        amount = request.form.get('amount')
        note = request.form.get('note', 'Quick Payment')
        
        try:
            amount_val = float(amount)
            # History ya transaction table me entry save karein
            conn.execute("""
                INSERT INTO history (user_id, title, description, amount, timestamp)
                VALUES (?, ?, ?, ?, DATETIME('now', 'localtime'))
            """, (user_id, f"Paid to {upi_id}", note, f"₹{amount_val}"))
            conn.commit()
            flash("Payment Successful!", "success")
        except Exception as e:
            flash("Payment Failed! Please check details.", "error")
            
        conn.close()
        return redirect(url_for('pay'))
        
    # User ki trips fetch karein jahan wo quick pay kar sake
    user_trips = conn.execute("""
        SELECT trips.id, trips.trip_name 
        FROM trips
        JOIN trip_members ON trips.id = trip_members.trip_id
        WHERE trip_members.user_id = ?
    """, (user_id,)).fetchall()
    
    conn.close()
    return render_template('pay.html', user_trips=user_trips)

# ---------------- WEALTH & FINANCIAL SUMMARY ----------------
@app.route('/wealth')
def wealth():
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    user_id = session["user_id"]
    conn = get_db()
    
    # 1. Overall total spent by user across all trips
    overall_spent = conn.execute("""
        SELECT SUM(amount) as total FROM expenses WHERE payer_id = ?
    """, (user_id,)).fetchone()["total"] or 0
    
    # 2. Total trips joined by user
    total_trips_joined = conn.execute("""
        SELECT COUNT(*) as cnt FROM trip_members WHERE user_id = ?
    """, (user_id,)).fetchone()["cnt"] or 0
    
    # 3. Total settlements or payments made
    total_payments = conn.execute("""
        SELECT COUNT(*) as cnt FROM history WHERE user_id = ? AND title LIKE '%Payment%'
    """, (user_id,)).fetchone()["cnt"] or 0
    
    # 4. Recent financial history/activity
    recent_activity = conn.execute("""
        SELECT title, description, amount, timestamp 
        FROM history 
        WHERE user_id = ? 
        ORDER BY timestamp DESC LIMIT 6
    """, (user_id,)).fetchall()
    
    conn.close()
    
    return render_template(
        'wealth.html', 
        overall_spent=overall_spent, 
        total_trips_joined=total_trips_joined, 
        total_payments=total_payments,
        recent_activity=recent_activity
    )
# ---------------- PHOTO MANAGEMENT ----------------
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
        
        conn.execute("""
            INSERT INTO history (user_id, title, description, amount)
            VALUES (?, ?, ?, ?)
        """, (session["user_id"], "Photo Uploaded", f"Uploaded {filename}", "Media"))

        conn.commit()
        conn.close()

    return redirect(url_for("view_trip", trip_id=trip_id))

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
        conn.execute("DELETE FROM photos WHERE id = ?", (photo["id"],))
        conn.commit()
        
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo_filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            
    conn.close()
    return redirect(url_for("view_trip", trip_id=trip_id))


# ---------------- CHATBOT & ANALYTICS APIs ----------------
@app.route('/api/chat', methods=['POST'])
def chat_api():
    global chat_session 
    try:
        data = request.get_json()
        user_message = data.get("user_message")
        trip_id = data.get("trip_id")  

        if not user_message:
            return jsonify({"error": "Message required"}), 400

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

        if chat_session:
            response = chat_session.send_message(ai_prompt)
            return jsonify({"bot_reply": response.text})
        else:
            return jsonify({"bot_reply": "AI Chatbot is currently inactive, but your data is safe!"})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/submit-crowd', methods=['POST'])
def submit_crowd():
    data = request.json
    location = data.get('location').lower()
    level = data.get('level') 
    
    conn = get_db()
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
        cat_data = conn.execute('''
            SELECT description, SUM(amount) as total 
            FROM expenses WHERE trip_id = ?
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

        trend_data = conn.execute('''
            SELECT description, amount FROM expenses 
            WHERE trip_id = ? ORDER BY id ASC
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
    
    time_score = 30 
    if 10 <= current_hour <= 17:
        time_score = 85 
    elif 17 < current_hour <= 21:
        time_score = 95 
    elif 7 <= current_hour < 10:
        time_score = 50 
        
    weather_multiplier = 1.0 
    weather_condition = "Clear"
    try:
        url = f"https://wttr.in/{location}?format=j1"
        res = requests.get(url, timeout=3).json()
        current_condition = res['current_condition'][0]['weatherDesc'][0]['value']
        
        if any(word in current_condition for word in ['Rain', 'Shower', 'Drizzle', 'Thunder']):
            weather_multiplier = 0.4  
            weather_condition = "Raining 🌧️️"
        elif any(word in current_condition for word in ['Cloud', 'Overcast', 'Fog', 'Mist']):
            weather_multiplier = 0.9  
            weather_condition = "Cloudy ☁️"
        elif any(word in current_condition for word in ['Clear', 'Sunny']):
            weather_multiplier = 1.1  
            weather_condition = "Clear & Sunny ☀️"
        else:
            weather_condition = current_condition
    except:
        weather_condition = "Clear (Default) 🌤️"
        
    conn = get_db()
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

    base_calc = (time_score * 0.3) + (user_score * 0.7)
    final_crowd_percentage = int(base_calc * weather_multiplier)
    final_crowd_percentage = max(10, min(100, final_crowd_percentage))

    return jsonify({
        "location": location_name.title(),
        "crowd_percentage": final_crowd_percentage,
        "weather": weather_condition,
        "reports_analyzed": len(recent_reports)
    })

# ---------------- LEAVE / DELETE TRIP ROUTE ----------------
@app.route('/leave-trip/<int:trip_id>', methods=['GET', 'POST'])
def leave_trip(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    user_id = session["user_id"]
    conn = get_db()
    
    # Trip se member ko remove karein
    conn.execute("DELETE FROM trip_members WHERE trip_id = ? AND user_id = ?", (trip_id, user_id))
    conn.execute("DELETE FROM expenses WHERE trip_id = ? AND payer_id = ?", (trip_id, user_id))
    conn.commit()
    conn.close()
    
    return redirect(url_for('dashboard'))


# ---------------- START SERVER ----------------
if __name__ == '__main__':
    with app.app_context():
        create_tables()
    app.run(host='0.0.0.0', port=5000, debug=True)