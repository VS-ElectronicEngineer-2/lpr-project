from __future__ import annotations
from flask import Flask, jsonify, request, render_template, redirect, url_for, session, flash
from datetime import datetime
from functools import wraps
import os
import pymysql
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

load_dotenv()

app = Flask(__name__, static_url_path='/static')

# ── Secrets / Config via ENV ────────────────────────────────────────────────
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))

app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "lpr_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "lpr_system")

# Optional shared token to protect ingest routes
SHARED_INGEST_TOKEN = os.getenv("SHARED_INGEST_TOKEN", "")

# ── DB helper ───────────────────────────────────────────────────────────────
def get_db():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor
    )

# ── Decorators ──────────────────────────────────────────────────────────────
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

def api_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return view(*args, **kwargs)
    return wrapped

def ingest_token_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if SHARED_INGEST_TOKEN:
            tok = request.headers.get("X-Auth-Token", "")
            if tok != SHARED_INGEST_TOKEN:
                return jsonify({"error": "invalid token"}), 401
        return view(*args, **kwargs)
    return wrapped

# ── Auth ────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","")
        password = request.form.get("password","")

        try:
            conn = get_db()
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
                user = cursor.fetchone()
        except Exception as e:
            print("DB error during login:", e)
            user = None
        finally:
            try:
                conn.close()
            except Exception:
                pass

        ok = False
        if user:
            # Prefer hashed column, but allow legacy plaintext if still in DB
            if "password_hash" in user and user["password_hash"]:
                ok = check_password_hash(user["password_hash"], password)
            elif "password" in user and user["password"]:
                ok = (password == user["password"])  # ⚠️ migrate to hashed ASAP

        if ok:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user.get("role", "user")
            return redirect(url_for("index"))
        else:
            flash("Invalid login credentials.")
            return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Pages ───────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("dashboard.html", username=session["username"])

# ── GPS APIs ────────────────────────────────────────────────────────────────
GPS_LOGS = []

@app.route("/gps-tracking")
@login_required
def gps_tracking():
    if GPS_LOGS:
        return jsonify(GPS_LOGS[-1])
    return jsonify({"error": "No GPS data"}), 404

@app.route("/gps-tracking-history")
@api_login_required
def gps_tracking_history():
    plate = request.args.get("plate")
    start = request.args.get("start")
    end = request.args.get("end")

    if not plate or not start or not end:
        return jsonify({"error": "Missing parameters"}), 400

    try:
        # Support DD/MM/YYYY
        if "/" in start:
            start = datetime.strptime(start, "%d/%m/%Y").strftime("%Y-%m-%d")
            end = datetime.strptime(end, "%d/%m/%Y").strftime("%Y-%m-%d")

        start_datetime = f"{start} 00:00:00"
        end_datetime = f"{end} 23:59:59"

        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT plate, latitude, longitude, speed, time AS timestamp
                FROM gps_logs
                WHERE UPPER(plate) = UPPER(%s) AND time BETWEEN %s AND %s
                ORDER BY time ASC
            """, (plate, start_datetime, end_datetime))
            results = cursor.fetchall()
        conn.close()

        return jsonify(results)
    except Exception as e:
        print("Error fetching GPS history:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/gps", methods=["POST"])
@ingest_token_required
def receive_gps():
    global GPS_LOGS
    data = request.json
    if not data:
        return jsonify({"error": "no data"}), 400

    # If plate/time missing, inject defaults
    data["plate"] = data.get("plate") or os.getenv("SCAN_CAR_PLATE", "VMD9454")
    data["time"] = data.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    GPS_LOGS.append(data)
    if len(GPS_LOGS) > 1000:
        GPS_LOGS = GPS_LOGS[-1000:]

    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO gps_logs (plate, latitude, longitude, speed, time)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                data.get("plate"),
                data.get("latitude"),
                data.get("longitude"),
                data.get("speed", 0),
                data.get("time")
            ))
        conn.close()
    except Exception as e:
        print("Failed to insert GPS:", e)

    return jsonify({"status": "received"}), 200

# ── Plate ingest / query ────────────────────────────────────────────────────
@app.route("/api/receive-plate", methods=["POST"])
@ingest_token_required
def receive_plate():
    data = request.get_json() or {}
    if not data:
        return jsonify({"error": "No data received"}), 400

    # Derive status for scofflaw, ensure snapshot URL
    if data.get("summons") and isinstance(data["summons"], list) and len(data["summons"]) > 0:
        data["status"] = data.get("status") or "Scofflaw"

    snapshot = data.get("snapshot", "")
    if not snapshot.startswith("http"):
        data["snapshot"] = "static/default-car.png"

    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO dashboard_plates (plate, status, snapshot, time, latitude, longitude, officer_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                data.get("plate"),
                data.get("status"),
                data.get("snapshot"),
                data.get("time"),
                data.get("latitude"),
                data.get("longitude"),
                data.get("officer_id")
            ))
        conn.close()
    except Exception as e:
        print("Failed to insert into dashboard_plates:", e)

    return jsonify({"status": "success"}), 200

@app.route("/api/received-plates", methods=["GET"])
@api_login_required
def get_received_plates():
    start = request.args.get("start")
    end = request.args.get("end")

    try:
        conn = get_db()
        with conn.cursor() as cursor:
            params = []
            query = "SELECT * FROM dashboard_plates"
            if start and end:
                query += " WHERE DATE(time) BETWEEN %s AND %s"
                params = [start, end]
            query += " ORDER BY id DESC"
            cursor.execute(query, params)
            rows = cursor.fetchall()
        conn.close()

        plates = []
        for row in rows:
            time_value = row["time"]
            formatted_time = time_value if isinstance(time_value, str) else (
                time_value.strftime("%Y-%m-%d %H:%M:%S") if time_value else ""
            )
            plates.append({
                "plate": row["plate"],
                "status": row["status"],
                "snapshot": row["snapshot"],
                "time": formatted_time,
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "officer_id": row["officer_id"]
            })

        return jsonify(plates)
    except Exception as e:
        print("Error retrieving plates:", e)
        return jsonify({"error": str(e)}), 500

# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5002"))
    app.run(host="0.0.0.0", port=port, debug=False)
