from __future__ import annotations

import json
import os
import secrets
import sqlite3
import string
from datetime import datetime
from pathlib import Path as FilePath

import requests
from flask import (
    Flask, flash, jsonify, redirect, render_template, request,
    session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# Optional live web search for the AI agent. The app still runs if this package
# is not installed; the web_search tool will return a helpful error instead.
try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None


# ---------------------------------------------------------------------------
# APP CONFIGURATION
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-development-secret")

BASE_DIR = FilePath(__file__).resolve().parent
DATABASE = os.environ.get("BHARATYATRA_DATABASE", str(BASE_DIR / "bharatyatra.db"))
UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_UPLOAD_SIZE = 8 * 1024 * 1024

app.config["DATABASE"] = DATABASE
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

chat_session = None
FX_CACHE: dict[str, float] = {}


# ---------------------------------------------------------------------------
# DATABASE HELPERS AND SAFE, NON-DESTRUCTIVE SCHEMA MIGRATION
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(app.config["DATABASE"], timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def table_columns(conn, table_name):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def ensure_column(conn, table_name, column_name, definition):
    if column_name not in table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def create_tables():
    """Create missing tables and add compatible columns without deleting user data."""
    conn = get_db()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT 'Traveler',
            email TEXT UNIQUE NOT NULL,
            password TEXT
        );

        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_name TEXT,
            trip_code TEXT UNIQUE,
            creator_id INTEGER,
            user_id INTEGER,
            title TEXT,
            destination TEXT,
            budget REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS trip_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(trip_id, user_id),
            FOREIGN KEY (trip_id) REFERENCES trips(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            payer_id INTEGER NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT 'Expense',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trip_id) REFERENCES trips(id),
            FOREIGN KEY (payer_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            uploader_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trip_id) REFERENCES trips(id),
            FOREIGN KEY (uploader_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS crowd_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_name TEXT NOT NULL,
            crowd_level INTEGER NOT NULL,
            report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT 'Activity',
            description TEXT NOT NULL DEFAULT '',
            amount TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS itineraries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL DEFAULT 1,
            title TEXT NOT NULL DEFAULT '',
            activities TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (trip_id) REFERENCES trips(id)
        );
        """)

        # Migrate columns in databases created by earlier versions of this app.
        ensure_column(conn, "users", "name", "TEXT NOT NULL DEFAULT 'Traveler'")
        ensure_column(conn, "users", "email", "TEXT")
        ensure_column(conn, "users", "password", "TEXT")

        for col, definition in {
            "trip_name": "TEXT",
            "trip_code": "TEXT",
            "creator_id": "INTEGER",
            "user_id": "INTEGER",
            "title": "TEXT",
            "destination": "TEXT",
            "budget": "REAL DEFAULT 0",
            "created_at": "TIMESTAMP",
        }.items():
            ensure_column(conn, "trips", col, definition)

        for col, definition in {
            "trip_id": "INTEGER",
            "payer_id": "INTEGER",
            "amount": "REAL NOT NULL DEFAULT 0",
            "description": "TEXT NOT NULL DEFAULT 'Expense'",
            "date": "TIMESTAMP",
        }.items():
            ensure_column(conn, "expenses", col, definition)

        # Older databases may have an expenses.date column with NULL values,
        # and migrated columns do not inherit the CREATE TABLE default.
        conn.execute(
            "UPDATE expenses SET date = CURRENT_TIMESTAMP WHERE date IS NULL OR TRIM(date) = ''"
        )

        for col, definition in {
            "user_id": "INTEGER",
            "title": "TEXT NOT NULL DEFAULT 'Activity'",
            "description": "TEXT NOT NULL DEFAULT ''",
            "amount": "TEXT",
            "timestamp": "TIMESTAMP",
        }.items():
            ensure_column(conn, "history", col, definition)

        # Keep older trip fields readable while adding the current template fields.
        old_trips = conn.execute("SELECT * FROM trips").fetchall()
        for trip in old_trips:
            trip_id = trip["id"]
            keys = set(trip.keys())
            trip_name = trip["trip_name"] if "trip_name" in keys else None
            title = trip["title"] if "title" in keys else None
            creator_id = trip["creator_id"] if "creator_id" in keys else None
            user_id = trip["user_id"] if "user_id" in keys else None
            trip_code = trip["trip_code"] if "trip_code" in keys else None
            name = trip_name or title or f"Trip {trip_id}"
            creator = creator_id or user_id
            if not trip_code:
                trip_code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
                while conn.execute(
                    "SELECT 1 FROM trips WHERE trip_code = ? AND id != ?",
                    (trip_code, trip_id)
                ).fetchone():
                    trip_code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            conn.execute("""
                UPDATE trips
                SET trip_name = COALESCE(NULLIF(trip_name, ''), ?),
                    title = COALESCE(NULLIF(title, ''), ?),
                    trip_code = COALESCE(NULLIF(trip_code, ''), ?),
                    creator_id = COALESCE(creator_id, ?),
                    user_id = COALESCE(user_id, creator_id, ?),
                    destination = COALESCE(NULLIF(destination, ''), 'Not specified'),
                    budget = COALESCE(budget, 0)
                WHERE id = ?
            """, (name, name, trip_code, creator, creator, trip_id))

        # Add creators and legacy owners as members. Ignore already-existing rows.
        conn.execute("""
            INSERT OR IGNORE INTO trip_members (trip_id, user_id)
            SELECT id, creator_id FROM trips WHERE creator_id IS NOT NULL
        """)
        conn.execute("""
            INSERT OR IGNORE INTO trip_members (trip_id, user_id)
            SELECT id, user_id FROM trips WHERE user_id IS NOT NULL
        """)
        conn.commit()
    finally:
        conn.close()


def get_trip_for_member(conn, trip_id, user_id):
    return conn.execute("""
        SELECT t.*
        FROM trips t
        JOIN trip_members tm ON tm.trip_id = t.id
        WHERE t.id = ? AND tm.user_id = ?
    """, (trip_id, user_id)).fetchone()


def add_history(conn, user_id, title, description, amount=""):
    conn.execute("""
        INSERT INTO history (user_id, title, description, amount, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, title, description, str(amount), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


# ---------------------------------------------------------------------------
# CURRENCY CONVERSION
# ---------------------------------------------------------------------------
def get_exchange_rate(from_curr, to_curr="INR"):
    from_curr = (from_curr or "INR").upper().strip()
    to_curr = (to_curr or "INR").upper().strip()
    if from_curr == to_curr:
        return 1.0

    cache_key = f"{from_curr}_{to_curr}"
    if cache_key in FX_CACHE:
        return FX_CACHE[cache_key]

    try:
        response = requests.get(
            f"https://open.er-api.com/v6/latest/{from_curr}", timeout=5
        )
        response.raise_for_status()
        data = response.json()
        if data.get("result") == "success":
            rate = data.get("rates", {}).get(to_curr)
            if rate is not None:
                FX_CACHE[cache_key] = float(rate)
                return float(rate)
    except (requests.RequestException, ValueError, TypeError) as exc:
        app.logger.warning("FX API unavailable: %s", exc)

    # Approximate fallback values only; these can be outdated.
    fallback = {"USD": 83.5, "EUR": 90.2, "AED": 22.7, "GBP": 105.0, "CAD": 61.5, "INR": 1.0}
    if from_curr in fallback and to_curr in fallback:
        return round(fallback[from_curr] / fallback[to_curr], 4)
    raise ValueError(f"Exchange rate unavailable for {from_curr} to {to_curr}.")


# ---------------------------------------------------------------------------
# AI AGENT TOOLS
# ---------------------------------------------------------------------------
def tool_add_expense(trip_id, user_id, description, amount, currency="INR"):
    try:
        amount = float(amount)
        if amount <= 0:
            return {"status": "error", "message": "Amount must be greater than zero."}
        rate = get_exchange_rate(currency, "INR")
    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}

    converted = round(amount * rate, 2)
    description = str(description or "Expense").strip()[:200]
    full_description = f"{description} ({amount:g} {currency.upper()})" if currency.upper() != "INR" else description

    conn = get_db()
    try:
        if not get_trip_for_member(conn, trip_id, user_id):
            return {"status": "error", "message": "Trip not found or access denied."}
        conn.execute(
            "INSERT INTO expenses (trip_id, payer_id, amount, description, date) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (trip_id, user_id, converted, full_description)
        )
        add_history(conn, user_id, "AI Agent Expense Logged", full_description, f"₹{converted:.2f}")
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "added_amount_inr": converted,
        "message": f"Logged {description}: ₹{converted:.2f}."
    }


def tool_get_trip_summary(trip_id, user_id):
    conn = get_db()
    try:
        if not get_trip_for_member(conn, trip_id, user_id):
            return {"status": "error", "message": "Trip not found or access denied."}
        total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE trip_id = ?",
            (trip_id,)
        ).fetchone()["total"]
        recent = conn.execute(
            "SELECT description, amount FROM expenses WHERE trip_id = ? ORDER BY id DESC LIMIT 5",
            (trip_id,)
        ).fetchall()
        return {
            "status": "success",
            "total_spent_inr": total,
            "recent_expenses": [f"{r['description']}: ₹{r['amount']:.2f}" for r in recent]
        }
    finally:
        conn.close()


def tool_create_roadmap(trip_id, user_id, days_plan):
    conn = get_db()
    try:
        if not get_trip_for_member(conn, trip_id, user_id):
            return {"status": "error", "message": "Trip not found or access denied."}
        if not isinstance(days_plan, list) or len(days_plan) > 30:
            return {"status": "error", "message": "Provide a list of up to 30 itinerary days."}
        conn.execute("DELETE FROM itineraries WHERE trip_id = ?", (trip_id,))
        for plan in days_plan:
            conn.execute("""
                INSERT INTO itineraries (trip_id, day_number, title, activities)
                VALUES (?, ?, ?, ?)
            """, (
                trip_id,
                int(plan.get("day", 1)),
                str(plan.get("title", ""))[:150],
                str(plan.get("activities", ""))[:3000]
            ))
        add_history(conn, user_id, "AI Travel Roadmap Created", f"{len(days_plan)}-day itinerary saved", "Roadmap")
        conn.commit()
        return {"status": "success", "message": f"Saved a {len(days_plan)}-day travel roadmap."}
    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": f"Invalid itinerary: {exc}"}
    finally:
        conn.close()


def tool_web_search(query):
    if DDGS is None:
        return {"status": "error", "message": "Optional package missing. Install duckduckgo-search to enable web search."}
    try:
        with DDGS() as search:
            results = list(search.text(keywords=str(query)[:300], max_results=3))
        if not results:
            return {"status": "error", "message": "No live results found."}
        return {
            "status": "success",
            "query": query,
            "web_data": "\n---\n".join(
                f"Source: {r.get('title', '')}\nDetails: {r.get('body', '')}"
                for r in results
            )
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


GEMINI_TOOLS = [{
    "function_declarations": [
        {
            "name": "add_expense",
            "description": "Logs an expense in INR after currency conversion.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "description": {"type": "STRING"},
                    "amount": {"type": "NUMBER"},
                    "currency": {"type": "STRING"}
                },
                "required": ["description", "amount"]
            }
        },
        {
            "name": "get_trip_summary",
            "description": "Returns the current trip expense total and recent expenses.",
            "parameters": {"type": "OBJECT", "properties": {}}
        },
        {
            "name": "create_travel_roadmap",
            "description": "Saves a multi-day travel itinerary for the current trip.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "days_plan": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "day": {"type": "INTEGER"},
                                "title": {"type": "STRING"},
                                "activities": {"type": "STRING"}
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
            "description": "Searches the web for travel facts.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"query": {"type": "STRING"}},
                "required": ["query"]
            }
        }
    ]
}]


def get_live_trip_context(trip_id, user_id):
    conn = get_db()
    try:
        trip = get_trip_for_member(conn, trip_id, user_id)
        if not trip:
            return None
        total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE trip_id = ?",
            (trip_id,)
        ).fetchone()["total"]
        recent = conn.execute(
            "SELECT description, amount FROM expenses WHERE trip_id = ? ORDER BY id DESC LIMIT 5",
            (trip_id,)
        ).fetchall()
        itinerary = conn.execute(
            "SELECT day_number, title FROM itineraries WHERE trip_id = ? ORDER BY day_number",
            (trip_id,)
        ).fetchall()
        return (
            f"Trip: {trip['trip_name']}; destination: {trip['destination'] or 'Not specified'}; "
            f"budget: ₹{trip['budget'] or 0}; total expenses: ₹{total}; "
            f"recent expenses: {[dict(r) for r in recent]}; "
            f"itinerary: {[dict(r) for r in itinerary]}"
        )
    finally:
        conn.close()


@app.route("/api/agent", methods=["POST"])
def agentic_ai():
    if "user_id" not in session:
        return jsonify({"reply": "Please log in to access the AI Agent."}), 401

    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()
    trip_id = data.get("trip_id")
    if not prompt or not trip_id:
        return jsonify({"reply": "A prompt and trip ID are required."}), 400

    try:
        trip_id = int(trip_id)
    except (TypeError, ValueError):
        return jsonify({"reply": "Invalid trip ID."}), 400

    context = get_live_trip_context(trip_id, session["user_id"])
    if context is None:
        return jsonify({"reply": "Trip not found or access denied."}), 403

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"reply": "AI is not configured yet. Set the GEMINI_API_KEY environment variable."}), 503

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-2.5-flash:generateContent"
    )
    url = f"{endpoint}?key={api_key}"
    system_instruction = (
        "You are Bharatyatra AI, a travel assistant. Use the active trip context "
        "when answering. Call add_expense only when the user explicitly asks to log "
        "an expense; call create_travel_roadmap only when asked to save an itinerary; "
        "use web_search for live travel facts. Be concise.\n\n" + context
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": GEMINI_TOOLS
    }

    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
        result = response.json()
        parts = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        function_part = next((part for part in parts if "functionCall" in part), None)
        if not function_part:
            answer = "".join(part.get("text", "") for part in parts).strip()
            return jsonify({"reply": answer or "The AI did not return a response."})

        call = function_part["functionCall"]
        name = call.get("name")
        args = call.get("args", {})
        if name == "add_expense":
            tool_result = tool_add_expense(
                trip_id, session["user_id"], args.get("description", "Expense"),
                args.get("amount", 0), args.get("currency", "INR")
            )
        elif name == "get_trip_summary":
            tool_result = tool_get_trip_summary(trip_id, session["user_id"])
        elif name == "create_travel_roadmap":
            tool_result = tool_create_roadmap(trip_id, session["user_id"], args.get("days_plan", []))
        elif name == "web_search":
            tool_result = tool_web_search(args.get("query", ""))
        else:
            return jsonify({"reply": "The AI requested an unsupported tool."}), 502

        second_payload = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]},
                {"role": "model", "parts": [{"functionCall": call}]},
                {"role": "user", "parts": [{
                    "text": "Tool result (JSON): " + json.dumps(tool_result, ensure_ascii=False)
                }]}
            ]
        }
        second_response = requests.post(url, json=second_payload, timeout=20)
        second_response.raise_for_status()
        second_data = second_response.json()
        final_parts = second_data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        final_text = "".join(p.get("text", "") for p in final_parts).strip()
        return jsonify({"reply": final_text or tool_result.get("message", "Tool completed."),
                        "tool_executed": name, "tool_result": tool_result})
    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        app.logger.exception("AI agent request failed: %s", exc)
        return jsonify({"reply": "AI request failed. Check your API key, internet connection, and server log."}), 502


# ---------------------------------------------------------------------------
# HOME AND AUTHENTICATION
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not name or not email or not password:
        flash("All fields are required.", "error")
        return redirect(url_for("register"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("register"))

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
            (name[:100], email[:254], generate_password_hash(password))
        )
        conn.commit()
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))
    except sqlite3.IntegrityError:
        flash("That email is already registered.", "error")
        return redirect(url_for("register"))
    finally:
        conn.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()

    if not user or not user["password"]:
        flash("Email is not registered or the account needs a password reset.", "error")
        return redirect(url_for("login"))
    if not check_password_hash(user["password"], password):
        flash("Incorrect email or password.", "error")
        return redirect(url_for("login"))

    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# DASHBOARD AND TRIPS
# ---------------------------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db()
    try:
        trips = conn.execute("""
            SELECT t.*
            FROM trips t
            JOIN trip_members tm ON t.id = tm.trip_id
            WHERE tm.user_id = ?
            ORDER BY t.id DESC
        """, (session["user_id"],)).fetchall()
    finally:
        conn.close()
    return render_template("dashboard.html", name=session.get("user_name", "Traveler"), trips=trips)


@app.route("/create-trip", methods=["POST"])
def create_trip():
    if "user_id" not in session:
        return redirect(url_for("login"))
    trip_name = request.form.get("trip_name", "").strip()
    if not trip_name:
        flash("Trip name is required.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    try:
        trip_code = None
        for _ in range(20):
            candidate = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            if not conn.execute("SELECT 1 FROM trips WHERE trip_code = ?", (candidate,)).fetchone():
                trip_code = candidate
                break
        if not trip_code:
            flash("Could not generate a trip code. Please try again.", "error")
            return redirect(url_for("dashboard"))

        cursor = conn.execute("""
            INSERT INTO trips (trip_name, trip_code, creator_id, user_id, title, destination, budget)
            VALUES (?, ?, ?, ?, ?, 'Not specified', 0)
        """, (trip_name[:120], trip_code, session["user_id"], session["user_id"], trip_name[:120]))
        trip_id = cursor.lastrowid
        conn.execute("INSERT OR IGNORE INTO trip_members (trip_id, user_id) VALUES (?, ?)",
                     (trip_id, session["user_id"]))
        add_history(conn, session["user_id"], "Trip Created", f"Created trip: {trip_name}", "Free")
        conn.commit()
        flash(f"Trip created. Share code: {trip_code}", "success")
        return redirect(url_for("view_trip", trip_id=trip_id))
    finally:
        conn.close()


@app.route("/join-trip", methods=["POST"])
def join_trip():
    if "user_id" not in session:
        return redirect(url_for("login"))
    trip_code = request.form.get("trip_code", "").strip().upper()
    if not trip_code:
        flash("Trip code is required.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    try:
        trip = conn.execute("SELECT * FROM trips WHERE trip_code = ?", (trip_code,)).fetchone()
        if not trip:
            flash("Invalid trip code.", "error")
            return redirect(url_for("dashboard"))
        existing = conn.execute(
            "SELECT 1 FROM trip_members WHERE trip_id = ? AND user_id = ?",
            (trip["id"], session["user_id"])
        ).fetchone()
        if existing:
            flash("You are already a member of this trip.", "info")
        else:
            conn.execute("INSERT INTO trip_members (trip_id, user_id) VALUES (?, ?)",
                         (trip["id"], session["user_id"]))
            add_history(conn, session["user_id"], "Joined Trip", f"Joined trip: {trip['trip_name']}", "Free")
            conn.commit()
            flash("You joined the trip.", "success")
        return redirect(url_for("view_trip", trip_id=trip["id"]))
    finally:
        conn.close()


@app.route("/trip/<int:trip_id>")
def view_trip(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db()
    try:
        trip = get_trip_for_member(conn, trip_id, session["user_id"])
        if not trip:
            flash("Trip not found or you are not a member.", "error")
            return redirect(url_for("dashboard"))

        members = conn.execute("""
            SELECT u.id, u.name, u.email
            FROM users u JOIN trip_members tm ON u.id = tm.user_id
            WHERE tm.trip_id = ? ORDER BY u.name
        """, (trip_id,)).fetchall()
        expenses = conn.execute("""
            SELECT e.id, e.amount, e.description,
                   COALESCE(e.date, CURRENT_TIMESTAMP) AS date, e.payer_id,
                   u.name AS payer_name
            FROM expenses e JOIN users u ON e.payer_id = u.id
            WHERE e.trip_id = ? ORDER BY e.date DESC, e.id DESC
        """, (trip_id,)).fetchall()
        total_expense = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE trip_id = ?",
            (trip_id,)
        ).fetchone()["total"]
        photos = conn.execute("""
            SELECT p.filename, p.uploader_id, p.upload_date, u.name AS uploader_name
            FROM photos p JOIN users u ON p.uploader_id = u.id
            WHERE p.trip_id = ? ORDER BY p.upload_date DESC
        """, (trip_id,)).fetchall()
        itineraries = conn.execute(
            "SELECT * FROM itineraries WHERE trip_id = ? ORDER BY day_number", (trip_id,)
        ).fetchall()
    finally:
        conn.close()

    # Both aliases are provided to keep older and newer templates working.
    trip_data = dict(trip)
    trip_data["name"] = trip_data.get("trip_name") or trip_data.get("title") or f"Trip {trip_id}"
    trip_data["code"] = trip_data.get("trip_code") or ""
    return render_template(
        "trip_detail.html", trip=trip_data, members=members, expenses=expenses,
        total_expense=total_expense, total_spent=total_expense, photos=photos,
        itineraries=itineraries
    )


# ---------------------------------------------------------------------------
# EXPENSES AND HISTORY
# ---------------------------------------------------------------------------
@app.route("/add-expense/<int:trip_id>", methods=["POST"])
def add_expense(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    description = request.form.get("description", "").strip()
    amount_raw = request.form.get("amount", "").strip()
    currency = request.form.get("currency", "INR").upper().strip()
    if not description or not amount_raw:
        flash("Amount and description are required.", "error")
        return redirect(url_for("view_trip", trip_id=trip_id))
    try:
        amount = float(amount_raw)
        if amount <= 0 or amount > 100000000:
            raise ValueError("Amount must be greater than zero and within the allowed limit.")
        rate = get_exchange_rate(currency, "INR")
    except (ValueError, TypeError) as exc:
        flash(str(exc) or "Invalid amount or currency.", "error")
        return redirect(url_for("view_trip", trip_id=trip_id))

    converted = round(amount * rate, 2)
    full_description = f"{description[:180]} ({amount:g} {currency})" if currency != "INR" else description[:200]
    history_amount = f"₹{converted:.2f}" if currency == "INR" else f"₹{converted:.2f} ({amount:g} {currency})"

    conn = get_db()
    try:
        if not get_trip_for_member(conn, trip_id, session["user_id"]):
            flash("Trip not found or access denied.", "error")
            return redirect(url_for("dashboard"))
        conn.execute("""
            INSERT INTO expenses (trip_id, payer_id, amount, description, date)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (trip_id, session["user_id"], converted, full_description))
        add_history(conn, session["user_id"], "Expense Added", full_description, history_amount)
        conn.commit()
        flash("Expense added successfully.", "success")
    finally:
        conn.close()
    return redirect(url_for("view_trip", trip_id=trip_id))


@app.route("/history")
def history():
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db()
    try:
        history_items = conn.execute(
            "SELECT * FROM history WHERE user_id = ? ORDER BY timestamp DESC, id DESC",
            (session["user_id"],)
        ).fetchall()
    finally:
        conn.close()
    return render_template("history.html", history_items=history_items)


# ---------------------------------------------------------------------------
# BALANCE, QUICK PAY LOG, AND WEALTH SUMMARY
# ---------------------------------------------------------------------------
@app.route("/balance")
def balance():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db()
    try:
        trips = conn.execute("""
            SELECT t.id, t.trip_name FROM trips t
            JOIN trip_members tm ON tm.trip_id = t.id
            WHERE tm.user_id = ? ORDER BY t.trip_name
        """, (user_id,)).fetchall()
        trip_balances = []
        total_overall_paid = 0.0
        for trip in trips:
            trip_id = trip["id"]
            total = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE trip_id = ?",
                (trip_id,)
            ).fetchone()["total"]
            member_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM trip_members WHERE trip_id = ?", (trip_id,)
            ).fetchone()["cnt"] or 1
            paid = conn.execute("""
                SELECT COALESCE(SUM(amount), 0) AS total FROM expenses
                WHERE trip_id = ? AND payer_id = ?
            """, (trip_id, user_id)).fetchone()["total"]
            total_overall_paid += paid
            trip_balances.append({
                "trip_name": trip["trip_name"], "trip_id": trip_id,
                "trip_total": total, "user_paid": paid,
                "fair_share": total / member_count,
                "balance_amount": paid - (total / member_count)
            })
    finally:
        conn.close()
    return render_template("balance.html", trip_balances=trip_balances,
                           total_overall_paid=total_overall_paid)


@app.route("/pay", methods=["GET", "POST"])
def pay():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    if request.method == "POST":
        upi_id = request.form.get("upi_id", "").strip()
        note = request.form.get("note", "Quick Payment").strip() or "Quick Payment"
        amount_raw = request.form.get("amount", "").strip()
        try:
            amount = float(amount_raw)
            if not upi_id or "@" not in upi_id:
                raise ValueError("Enter a valid-looking UPI ID.")
            if amount <= 0 or amount > 10000000:
                raise ValueError("Amount must be greater than zero.")
        except (ValueError, TypeError) as exc:
            flash(str(exc) or "Please check payment details.", "error")
            return redirect(url_for("pay"))

        # This is a record only. It does not send money or verify UPI payment.
        conn = get_db()
        try:
            add_history(conn, user_id, f"Payment Record for {upi_id}",
                        f"{note} — payment not processed by this app", f"₹{amount:.2f}")
            conn.commit()
        finally:
            conn.close()
        flash("Payment record saved only. No money was transferred by this app.", "info")
        return redirect(url_for("pay"))

    conn = get_db()
    try:
        user_trips = conn.execute("""
            SELECT t.id, t.trip_name FROM trips t
            JOIN trip_members tm ON tm.trip_id = t.id
            WHERE tm.user_id = ? ORDER BY t.trip_name
        """, (user_id,)).fetchall()
    finally:
        conn.close()
    return render_template("pay.html", user_trips=user_trips)


@app.route("/wealth")
def wealth():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db()
    try:
        overall_spent = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE payer_id = ?",
            (user_id,)
        ).fetchone()["total"]
        total_trips_joined = conn.execute(
            "SELECT COUNT(*) AS cnt FROM trip_members WHERE user_id = ?", (user_id,)
        ).fetchone()["cnt"]
        total_payments = conn.execute(
            "SELECT COUNT(*) AS cnt FROM history WHERE user_id = ? AND title LIKE '%Payment%'",
            (user_id,)
        ).fetchone()["cnt"]
        recent_activity = conn.execute("""
            SELECT title, description, amount, timestamp FROM history
            WHERE user_id = ? ORDER BY timestamp DESC, id DESC LIMIT 6
        """, (user_id,)).fetchall()
    finally:
        conn.close()
    return render_template("wealth.html", overall_spent=overall_spent,
                           total_trips_joined=total_trips_joined,
                           total_payments=total_payments, recent_activity=recent_activity)


# ---------------------------------------------------------------------------
# PHOTO MANAGEMENT
# ---------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/upload-photo/<int:trip_id>", methods=["POST"])
def upload_photo(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    file = request.files.get("photo")
    if not file or not file.filename:
        flash("Choose a photo to upload.", "error")
        return redirect(url_for("view_trip", trip_id=trip_id))
    if not allowed_file(file.filename):
        flash("Allowed photo formats: PNG, JPG, JPEG, GIF, WEBP.", "error")
        return redirect(url_for("view_trip", trip_id=trip_id))

    conn = get_db()
    saved_path = None
    try:
        if not get_trip_for_member(conn, trip_id, session["user_id"]):
            flash("Trip not found or access denied.", "error")
            return redirect(url_for("dashboard"))
        safe_name = secure_filename(file.filename)
        unique_name = f"{secrets.token_hex(8)}_{safe_name}"
        saved_path = UPLOAD_FOLDER / unique_name
        file.save(saved_path)
        conn.execute(
            "INSERT INTO photos (trip_id, uploader_id, filename) VALUES (?, ?, ?)",
            (trip_id, session["user_id"], unique_name)
        )
        add_history(conn, session["user_id"], "Photo Uploaded", f"Uploaded {safe_name}", "Media")
        conn.commit()
        flash("Photo uploaded.", "success")
    except Exception:
        if saved_path and saved_path.exists():
            saved_path.unlink()
        app.logger.exception("Photo upload failed")
        flash("Photo upload failed.", "error")
    finally:
        conn.close()
    return redirect(url_for("view_trip", trip_id=trip_id))


@app.route("/delete-photo/<int:trip_id>/<path:photo_filename>", methods=["POST"])
def delete_photo(trip_id, photo_filename):
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db()
    try:
        photo = conn.execute("""
            SELECT * FROM photos WHERE filename = ? AND uploader_id = ? AND trip_id = ?
        """, (photo_filename, session["user_id"], trip_id)).fetchone()
        if photo:
            conn.execute("DELETE FROM photos WHERE id = ?", (photo["id"],))
            conn.commit()
            filepath = UPLOAD_FOLDER / photo["filename"]
            if filepath.exists() and filepath.is_file():
                filepath.unlink()
            flash("Photo deleted.", "success")
        else:
            flash("Photo not found or you do not have permission to delete it.", "error")
    finally:
        conn.close()
    return redirect(url_for("view_trip", trip_id=trip_id))


# ---------------------------------------------------------------------------
# CHATBOT, ANALYTICS, AND CROWD APIs
# ---------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def chat_api():
    if "user_id" not in session:
        return jsonify({"error": "Please log in."}), 401
    data = request.get_json(silent=True) or {}
    user_message = str(data.get("user_message", "")).strip()
    trip_id = data.get("trip_id")
    if not user_message or not trip_id:
        return jsonify({"error": "Message and trip ID are required."}), 400
    try:
        trip_id = int(trip_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid trip ID."}), 400

    conn = get_db()
    try:
        if not get_trip_for_member(conn, trip_id, session["user_id"]):
            return jsonify({"error": "Trip not found or access denied."}), 403
        expenses = conn.execute("""
            SELECT e.description, e.amount, u.name AS payer
            FROM expenses e JOIN users u ON e.payer_id = u.id
            WHERE e.trip_id = ? ORDER BY e.id DESC
        """, (trip_id,)).fetchall()
        expense_text = "\n".join(
            f"- {e['payer']} paid ₹{e['amount']:.2f} for {e['description']}"
            for e in expenses
        ) or "No expenses logged yet."
    finally:
        conn.close()

    prompt = (
        "You are a trip expense assistant. Answer only from this expense data; "
        "say when the data is insufficient. Keep it concise.\n"
        f"Expense data:\n{expense_text}\nQuestion: {user_message}"
    )
    if chat_session:
        try:
            answer = chat_session.send_message(prompt)
            return jsonify({"bot_reply": answer.text})
        except Exception:
            app.logger.exception("Chat session failed")
    return jsonify({"bot_reply": "AI chatbot is not configured. Your trip data is saved safely."})


@app.route("/analytics/<int:trip_id>")
def analytics(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db()
    try:
        if not get_trip_for_member(conn, trip_id, session["user_id"]):
            flash("Trip not found or access denied.", "error")
            return redirect(url_for("dashboard"))
    finally:
        conn.close()
    return render_template("analytics.html", trip_id=trip_id)


@app.route("/api/real-analytics-data/<int:trip_id>")
def real_analytics_data(trip_id):
    if "user_id" not in session:
        return jsonify({"status": "error", "message": "Please log in."}), 401
    conn = get_db()
    try:
        if not get_trip_for_member(conn, trip_id, session["user_id"]):
            return jsonify({"status": "error", "message": "Trip not found or access denied."}), 403
        categories = conn.execute("""
            SELECT description, SUM(amount) AS total FROM expenses
            WHERE trip_id = ? GROUP BY description ORDER BY total DESC
        """, (trip_id,)).fetchall()
        members = conn.execute("""
            SELECT u.name AS real_name, SUM(e.amount) AS total
            FROM expenses e JOIN users u ON e.payer_id = u.id
            WHERE e.trip_id = ? GROUP BY e.payer_id ORDER BY total DESC
        """, (trip_id,)).fetchall()
        trends = conn.execute("""
            SELECT date, amount FROM expenses WHERE trip_id = ? ORDER BY id ASC
        """, (trip_id,)).fetchall()
        return jsonify({
            "status": "success",
            "categories": {
                "labels": [r["description"] for r in categories],
                "values": [r["total"] for r in categories]
            },
            "members": {
                "names": [r["real_name"] for r in members],
                "amounts": [r["total"] for r in members]
            },
            "daily": {
                "dates": [r["date"] or f"Entry {i + 1}" for i, r in enumerate(trends)],
                "totals": [r["amount"] for r in trends]
            }
        })
    finally:
        conn.close()


@app.route("/api/submit-crowd", methods=["POST"])
def submit_crowd():
    if "user_id" not in session:
        return jsonify({"status": "error", "message": "Please log in."}), 401
    data = request.get_json(silent=True) or {}
    location = str(data.get("location", "")).strip().lower()[:150]
    try:
        level = int(data.get("level"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Crowd level must be a number from 0 to 100."}), 400
    if not location or not 0 <= level <= 100:
        return jsonify({"status": "error", "message": "Location is required and level must be 0–100."}), 400
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO crowd_reports (location_name, crowd_level) VALUES (?, ?)",
            (location, level)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "success", "message": "Crowd report saved. It has not been independently verified."})


@app.route("/api/get-crowd/<path:location_name>", methods=["GET"])
def get_crowd(location_name):
    location = location_name.strip().lower()[:150]
    if not location:
        return jsonify({"error": "Location is required."}), 400

    hour = datetime.now().hour
    time_score = 30 if hour < 7 else 50 if hour < 10 else 85 if hour <= 17 else 95 if hour <= 21 else 30
    weather_multiplier = 1.0
    weather_condition = "Weather unavailable"
    try:
        response = requests.get(f"https://wttr.in/{requests.utils.quote(location)}?format=j1", timeout=4)
        response.raise_for_status()
        condition = response.json()["current_condition"][0]["weatherDesc"][0]["value"]
        weather_condition = condition
        lower = condition.lower()
        if any(word in lower for word in ("rain", "shower", "drizzle", "thunder")):
            weather_multiplier = 0.4
        elif any(word in lower for word in ("cloud", "overcast", "fog", "mist")):
            weather_multiplier = 0.9
        elif any(word in lower for word in ("clear", "sunny")):
            weather_multiplier = 1.1
    except Exception:
        pass

    conn = get_db()
    try:
        reports = conn.execute("""
            SELECT crowd_level FROM crowd_reports
            WHERE location_name = ? ORDER BY report_time DESC LIMIT 5
        """, (location,)).fetchall()
    finally:
        conn.close()
    user_score = sum(r["crowd_level"] for r in reports) / len(reports) if reports else time_score
    estimate = max(0, min(100, int(((time_score * 0.3) + (user_score * 0.7)) * weather_multiplier)))
    return jsonify({
        "location": location_name.title(),
        "crowd_percentage": estimate,
        "weather": weather_condition,
        "reports_analyzed": len(reports),
        "note": "Estimate based on recent user reports, time, and available weather data; not an official measurement."
    })


# ---------------------------------------------------------------------------
# LEAVE TRIP
# ---------------------------------------------------------------------------
@app.route("/leave-trip/<int:trip_id>", methods=["POST", "GET"])
def leave_trip(trip_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db()
    try:
        membership = conn.execute(
            "SELECT 1 FROM trip_members WHERE trip_id = ? AND user_id = ?",
            (trip_id, user_id)
        ).fetchone()
        if not membership:
            flash("You are not a member of this trip.", "error")
            return redirect(url_for("dashboard"))
        conn.execute("DELETE FROM trip_members WHERE trip_id = ? AND user_id = ?", (trip_id, user_id))
        # Keep expense/history records intact so that the trip's shared ledger is not silently changed.
        add_history(conn, user_id, "Left Trip", f"Left trip ID: {trip_id}", "")
        conn.commit()
        flash("You left the trip. Existing expense records were kept.", "info")
    finally:
        conn.close()
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
create_tables()

if __name__ == "__main__":
    # Debug mode is intended only for local development.
    app.run(host='0.0.0.0', port=5000, debug=True)