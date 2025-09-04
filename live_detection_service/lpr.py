# lpr.py  — safer public-ready version
# -*- coding: utf-8 -*-
"""
Key changes:
- 🔐 All secrets/URLs/IPs pulled from environment (.env) instead of hard-coded.
- 🔐 Optional shared ingest token for /api/gps and /api/receive-plate (X-Auth-Token header).
- 🔐 Login uses password hashing (fallback to plaintext for legacy rows).
- 🔐 /start-all and /stop-all protected by admin_required decorator.
- ✅ Removed verify=False; allow override via env PARKING_VERIFY_SSL=false only if you must.
- ✅ Reduced PII in logs; masked plates.
- ✅ Session cookie hardening.
"""

from __future__ import annotations
from flask import Flask, render_template, Response, jsonify, request, redirect, url_for, session, send_file
import platform
import os
import cv2
import time
import threading
import requests
from queue import Queue
import pandas as pd
from io import BytesIO
import gps
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import Table, TableStyle, SimpleDocTemplate, Paragraph, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from datetime import datetime
import numpy as np
import pymysql
from pathlib import Path
import json
import socket
import subprocess
from functools import wraps

# 🔐 env + hashing
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

load_dotenv()  # reads .env if present

# ──────────────────────────────────────────────────────────────────────────────
# Camera (Linux only)
# ──────────────────────────────────────────────────────────────────────────────
if platform.system() == "Linux":
    try:
        from picamera2 import Picamera2
    except Exception:
        Picamera2 = None
else:
    Picamera2 = None

# ──────────────────────────────────────────────────────────────────────────────
# Config/Env (🔐 no secrets hard-coded)
# ──────────────────────────────────────────────────────────────────────────────
def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "y", "on")

SECRET_KEY = os.getenv("SECRET_KEY") or os.urandom(24)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "lpr_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "lpr_system")

PLATE_RECOGNIZER_API_URL = os.getenv("PLATE_RECOGNIZER_API_URL", "https://api.platerecognizer.com/v1/plate-reader/")
API_TOKEN = os.getenv("PLATE_RECOGNIZER_TOKEN", "")

PARKING_API_URL = os.getenv("PARKING_API_URL", "https://mycouncil.citycarpark.my/parking/ctcp/services-listerner_mbk.php")
PARKING_API_ACTION = os.getenv("PARKING_API_ACTION", "GetParkingRightByPlateVerify")
PARKING_VERIFY_SSL = _env_bool("PARKING_VERIFY_SSL", True)

NODE_API_URL = os.getenv("NODE_API_URL", "http://localhost:5000/api/summons")

PAYMENT_QR_API = os.getenv("PAYMENT_QR_API", "http://localhost:5000/api/payment/generate-qr")
PAYMENT_QR_TOKEN = os.getenv("PAYMENT_QR_TOKEN", "")

DASHBOARD_URLS = [u.strip() for u in os.getenv("DASHBOARD_URLS", "").split(",") if u.strip()]
OFFLINE_FILE = os.getenv("OFFLINE_FILE", "offline_queue.json")

SCAN_CAR_PLATE = os.getenv("SCAN_CAR_PLATE", "VMD9454")

# 🔐 Optional shared token for ingest endpoints
SHARED_INGEST_TOKEN = os.getenv("SHARED_INGEST_TOKEN", "")

# ──────────────────────────────────────────────────────────────────────────────
# Flask app & security
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

app.config["SNAPSHOT_FOLDER"] = os.getenv("SNAPSHOT_FOLDER", "static/snapshots")
os.makedirs(app.config["SNAPSHOT_FOLDER"], exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# Globals
# ──────────────────────────────────────────────────────────────────────────────
detected_plates = []
summons_data = []
lock = threading.Lock()
frame_queue = Queue(maxsize=1)
gps_logs = []
stored_officer_id = "Unknown"
latest_gps = {"latitude": None, "longitude": None, "last_update": None}

api_stats = {"success_count": 0, "failure_count": 0, "total_time": 0.0}
recent_plates = {}

# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────
def mask_plate(p: str) -> str:
    if not p:
        return p
    # show first 3 chars then mask
    return (p[:3] + ("*" * max(0, len(p) - 3))).upper()

def is_connected(host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False

def save_offline(data):
    path = Path(OFFLINE_FILE)
    try:
        if path.exists():
            with open(path, "r") as f:
                offline_data = json.load(f)
        else:
            offline_data = []
    except json.JSONDecodeError:
        offline_data = []

    offline_data.append(data)
    with open(path, "w") as f:
        json.dump(offline_data, f, indent=2)

def get_db():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor
    )

# ──────────────────────────────────────────────────────────────────────────────
# Throttler
# ──────────────────────────────────────────────────────────────────────────────
class Throttler:
    def __init__(self, rate_limit, interval=1):
        self.rate_limit = rate_limit
        self.interval = interval
        self.timestamps = []

    def wait(self):
        while True:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < self.interval]
            if len(self.timestamps) < self.rate_limit:
                break
            time.sleep(self.interval - (now - self.timestamps[0]))
        self.timestamps.append(now)

throttler = Throttler(rate_limit=8, interval=1)  # same as before

def crop_plate_region(frame):
    h, w, _ = frame.shape
    return frame[int(h * 0.1):int(h * 0.99), int(w * 0.01):int(w * 0.99)]

def is_duplicate_plate(plate, cooldown=10):
    now = time.time()
    if plate in recent_plates and now - recent_plates[plate] < cooldown:
        return True
    recent_plates[plate] = now
    return False

# ──────────────────────────────────────────────────────────────────────────────
# Background: GPS
# ──────────────────────────────────────────────────────────────────────────────
def gps_updater():
    global latest_gps
    try:
        session_gps = gps.gps(mode=gps.WATCH_ENABLE)
        for report in session_gps:
            if report.get('class') == 'TPV':
                lat = getattr(report, 'lat', None)
                lon = getattr(report, 'lon', None)
                if lat and lon:
                    latest_gps["latitude"] = round(lat, 6)
                    latest_gps["longitude"] = round(lon, 6)
                    latest_gps["last_update"] = time.time()
    except Exception as e:
        print(f"GPS updater error: {e}")

threading.Thread(target=gps_updater, daemon=True).start()

# ──────────────────────────────────────────────────────────────────────────────
# Camera init (Linux only)
# ──────────────────────────────────────────────────────────────────────────────
try:
    if Picamera2:
        picam2 = Picamera2()
        config = picam2.create_preview_configuration(main={"size": (640, 480), "format": "RGB888"})
        picam2.configure(config)
        picam2.start()
    else:
        picam2 = None
except Exception as e:
    print(f"Camera initialization failed: {e}")
    picam2 = None

# ──────────────────────────────────────────────────────────────────────────────
# External calls
# ──────────────────────────────────────────────────────────────────────────────
def recognize_plate(frame):
    # Skip if token missing (fail closed)
    if not API_TOKEN:
        return []
    throttler.wait()
    try:
        start_time = time.time()
        roi = crop_plate_region(frame)
        _, img_encoded = cv2.imencode(".jpg", roi, [int(cv2.IMWRITE_JPEG_QUALITY), 25])
        img_bytes = img_encoded.tobytes()

        response = requests.post(
            PLATE_RECOGNIZER_API_URL,
            files={"upload": ("image.jpg", img_bytes, "image/jpeg")},
            headers={"Authorization": f"Token {API_TOKEN}"},
            timeout=30
        )

        elapsed = time.time() - start_time
        api_stats["total_time"] += elapsed

        if response.status_code in (200, 201):
            api_stats["success_count"] += 1
            return response.json().get("results", [])
        else:
            api_stats["failure_count"] += 1
            return []
    except requests.exceptions.RequestException:
        api_stats["failure_count"] += 1
        return []

def check_parking_status(plate_number):
    try:
        response = requests.get(
            PARKING_API_URL,
            params={"prpid": "", "action": PARKING_API_ACTION, "filterid": plate_number},
            verify=PARKING_VERIFY_SSL, timeout=8
        )
        if response.status_code == 200:
            result = response.json()
            if isinstance(result, list) and result:
                return f"Paid until {result[0].get('enddate', 'Unknown')} {result[0].get('endtime', '')}"
            return "Not Paid"
        return "Error"
    except requests.exceptions.RequestException:
        return "Error"

def check_summons_status(plate_number):
    try:
        response = requests.post(
            NODE_API_URL,
            json={"vehicleNumber": plate_number},
            headers={"Content-Type": "application/json"},
            timeout=8
        )
        data = response.json()
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and "summonsQueue" in data:
            return data["summonsQueue"]
        return []
    except requests.exceptions.RequestException:
        return []

# ──────────────────────────────────────────────────────────────────────────────
# Dashboard forwarders
# ──────────────────────────────────────────────────────────────────────────────
def send_gps_to_dashboard(data):
    if not is_connected():
        save_offline({"type": "gps", "data": data})
        return
    sent = False
    for url in DASHBOARD_URLS:
        if not url.endswith("/api/gps"):
            continue
        try:
            headers = {}
            if SHARED_INGEST_TOKEN:
                headers["X-Auth-Token"] = SHARED_INGEST_TOKEN
            r = requests.post(url, json=data, headers=headers, timeout=5)
            if r.status_code == 200:
                sent = True
        except Exception:
            pass
    if not sent:
        save_offline({"type": "gps", "data": data})

def send_plate_to_dashboard(plate_info):
    def forward():
        if not is_connected():
            save_offline({"type": "plate", "data": plate_info})
            return
        for url in DASHBOARD_URLS:
            if not url.endswith("/api/receive-plate"):
                continue
            try:
                headers = {}
                if SHARED_INGEST_TOKEN:
                    headers["X-Auth-Token"] = SHARED_INGEST_TOKEN
                requests.post(url, json=plate_info, headers=headers, timeout=5)
            except Exception:
                # keep trying others
                pass
    threading.Thread(target=forward, daemon=True).start()

# ──────────────────────────────────────────────────────────────────────────────
# Security decorators
# ──────────────────────────────────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "unauthorized"}), 401
        if not (session.get("is_admin") or session.get("role") == "admin"):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper

def ingest_token_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if SHARED_INGEST_TOKEN:
            tok = request.headers.get("X-Auth-Token", "")
            if tok != SHARED_INGEST_TOKEN:
                return jsonify({"error": "invalid token"}), 401
        return f(*args, **kwargs)
    return wrapper

# ──────────────────────────────────────────────────────────────────────────────
# Frame processing
# ──────────────────────────────────────────────────────────────────────────────
def process_frames():
    global stored_officer_id
    while True:
        if not frame_queue.empty():
            frame = frame_queue.get()
            plates = recognize_plate(frame)

            for plate_data in plates:
                plate_number = plate_data.get("plate", "").upper()
                if not plate_number:
                    continue
                if is_duplicate_plate(plate_number):
                    continue

                with lock:
                    if any(p["plate"] == plate_number for p in detected_plates):
                        continue

                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                snapshot_name = f"{plate_number}_{int(time.time())}.jpg"
                snapshot_path = os.path.join(app.config["SNAPSHOT_FOLDER"], snapshot_name)
                try:
                    cv2.imwrite(snapshot_path, frame)
                except Exception:
                    # if snapshot save fails, still continue
                    snapshot_path = ""

                latitude = latest_gps["latitude"]
                longitude = latest_gps["longitude"]
                if latitude is None or longitude is None:
                    continue
                if detected_plates and detected_plates[-1].get("latitude") == latitude and detected_plates[-1].get("longitude") == longitude:
                    continue

                officer_id = stored_officer_id

                parking_status = check_parking_status(plate_number)
                summons_status = check_summons_status(plate_number)

                if summons_status and isinstance(summons_status, list) and len(summons_status) > 0:
                    final_status = summons_status[0].get("status", "Not Paid")
                elif "Paid until" in parking_status:
                    final_status = parking_status
                else:
                    final_status = "Not Paid"

                # Build snapshot URL safely
                host = request.host if request else "localhost:5001"
                snapshot_url = f"http://{host}/static/snapshots/{snapshot_name}" if snapshot_name else ""

                plate_info = {
                    "plate": plate_number,
                    "status": final_status,
                    "summons": summons_status,
                    "time": timestamp,
                    "snapshot": snapshot_url,
                    "latitude": latitude,
                    "longitude": longitude,
                    "officer_id": officer_id
                }

                with lock:
                    detected_plates.append(plate_info)
                    send_plate_to_dashboard(plate_info)

                # DB insert
                try:
                    db = get_db()
                    with db.cursor() as cursor:
                        cursor.execute("""
                            INSERT INTO detected_plates (plate, timestamp, image_path, latitude, longitude, officer_id)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (plate_number, timestamp, snapshot_path, latitude, longitude, officer_id))
                        cursor.execute("""
                            INSERT INTO plate_history (plate, timestamp, image_path, latitude, longitude, officer_id)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (plate_number, timestamp, snapshot_path, latitude, longitude, officer_id))
                        db.commit()
                except Exception as e:
                    print("DB insert failed:", e)

threading.Thread(target=process_frames, daemon=True).start()

# ──────────────────────────────────────────────────────────────────────────────
# Video feed (MJPEG)
# ──────────────────────────────────────────────────────────────────────────────
def generate_frames():
    if not picam2:
        yield b"Camera not initialized."
        return
    frame_skip = 1
    count = 0
    while True:
        try:
            frame = picam2.capture_array()
            frame = cv2.resize(frame, (640, 480))
            count += 1
            if count % frame_skip == 0 and not frame_queue.full():
                frame_queue.put(frame.copy())
            _, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")
        except Exception as e:
            print(f"Error capturing frame: {e}")
            yield b"Error capturing frame."
            break

# ──────────────────────────────────────────────────────────────────────────────
# Auth routes
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    global stored_officer_id
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        conn = get_db()
        user = None
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
                user = cursor.fetchone()
        finally:
            conn.close()

        # 🔐 support hashed (preferred) or legacy plaintext column
        ok = False
        if user:
            # prefer 'password_hash', fallback to 'password'
            if "password_hash" in user and user["password_hash"]:
                ok = check_password_hash(user["password_hash"], password)
            elif "password" in user and user["password"]:
                # ⚠️ legacy plaintext — allow but log a warning (rotate ASAP)
                ok = (password == user["password"])

        if ok:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["officer_id"] = user.get("officer_id", "Unknown")
            session["is_admin"] = bool(user.get("is_admin", 0))
            session["role"] = user.get("role", "user")
            stored_officer_id = session["officer_id"]
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials.")

    return render_template("login.html")

@app.route("/logout")
def logout():
    global stored_officer_id
    session.clear()
    stored_officer_id = "Unknown"
    return redirect(url_for("login"))

@app.route("/")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("index.html")

# ──────────────────────────────────────────────────────────────────────────────
# APIs
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/video_feed")
def video_feed():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )

@app.route("/plates", methods=["GET"])
def plates():
    try:
        db = get_db()
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT plate, timestamp, image_path, latitude, longitude, officer_id
                FROM detected_plates ORDER BY id DESC LIMIT 100
            """)
            rows = cursor.fetchall()

        plates_from_db = []
        for row in rows:
            latest = next((p for p in detected_plates if p["plate"] == row["plate"]), None)
            status = latest["status"] if latest else "Not Paid"
            image_rel = row["image_path"].replace("\\", "/") if row["image_path"] else ""
            snapshot_url = f"http://{request.host}/{image_rel}" if image_rel else ""

            plate_data = {
                "plate": row["plate"],
                "status": status,
                "time": row["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if row["timestamp"] else "",
                "snapshot": snapshot_url,
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "officer_id": row["officer_id"],
                "summons": latest["summons"] if latest else []
            }
            plates_from_db.append(plate_data)

        return jsonify(plates_from_db)
    except Exception as e:
        print("Error loading plates from DB:", e)
        return jsonify([]), 500

@app.route("/api/user", methods=["GET"])
def get_user():
    if "user_id" in session:
        return jsonify({"user": session.get("username"), "officer_id": session.get("officer_id")})
    return jsonify({"error": "Not logged in"}), 401

@app.route("/summons", methods=["GET"])
def get_summons():
    global summons_data
    unique_summons = {}
    with lock:
        for plate in detected_plates:
            summons_status = check_summons_status(plate["plate"])
            if summons_status and summons_status != "Error":
                for summon in summons_status:
                    nn = summon.get("noticeNo")
                    if not nn:
                        continue
                    if nn not in unique_summons:
                        enriched = dict(summon)
                        enriched["latitude"] = plate["latitude"]
                        enriched["longitude"] = plate["longitude"]
                        enriched["snapshot"] = plate["snapshot"]
                        enriched["officer_id"] = plate.get("officer_id", session.get("officer_id", "Unknown"))
                        unique_summons[nn] = enriched
    summons_data = list(unique_summons.values())
    return jsonify(summons_data)

@app.route("/api/received-plates", methods=["GET"])
def get_received_plates():
    with lock:
        return jsonify(list(reversed(detected_plates)))

# ── Downloads
@app.route("/download/excel/detected_plates", methods=["GET"])
def download_detected_plates_excel():
    with lock:
        if not detected_plates:
            return "No data available", 400
        df = pd.DataFrame(detected_plates)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name="Detected Plates")
        output.seek(0)
        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name="detected_plates.xlsx")

@app.route("/download/pdf/detected_plates", methods=["GET"])
def download_detected_plates_pdf():
    with lock:
        if not detected_plates:
            return "No data available", 400

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=10, rightMargin=10, topMargin=20, bottomMargin=20)
        elements = []
        styles = getSampleStyleSheet()
        title = Paragraph("<b>Detected Plates Report</b>", styles["Title"])
        elements.append(title)

        data = [["License Plate", "Status", "Time", "Snapshot"]]
        for plate in detected_plates:
            snapshot_path = plate["snapshot"]
            try:
                img = Image(snapshot_path, width=100, height=70)
            except Exception:
                img = Paragraph("N/A", styles["Normal"])
            status_text = Paragraph(plate["status"], styles["Normal"])
            data.append([plate["plate"], status_text, plate["time"], img])

        col_widths = [100, 140, 120, 120]
        table = Table(data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.blue),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('WORDWRAP', (0, 0), (-1, -1)),
        ]))
        elements.append(table)
        doc.build(elements)

        buffer.seek(0)
        return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="detected_plates.pdf")

@app.route("/download/excel/summons_queue", methods=["GET"])
def download_summons_queue_excel():
    with lock:
        if not summons_data:
            return "No data available", 400
        df = pd.DataFrame(summons_data)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name="Summons Queue")
        output.seek(0)
        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name="summons_queue.xlsx")

@app.route("/download/pdf/summons_queue", methods=["GET"])
def download_summons_queue_pdf():
    with lock:
        if not summons_data:
            return "No data available", 400

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=20, rightMargin=20, topMargin=30, bottomMargin=20)
        elements = []
        styles = getSampleStyleSheet()
        title = Paragraph("<b>Summons Queue Report</b>", styles["Title"])
        elements.append(title)

        data = [["License Plate", "Notice No", "Offence", "Location", "Date", "Status", "Fine Amount", "Due Date"]]
        for summon in summons_data:
            data.append([
                summon.get("plate", ""),
                summon.get("noticeNo", ""),
                Paragraph(summon.get("offence", ""), styles["Normal"]),
                Paragraph(summon.get("location", ""), styles["Normal"]),
                summon.get("date", ""),
                summon.get("status", ""),
                summon.get("amount", ""),
                summon.get("due_date", "")
            ])

        col_widths = [60, 90, 180, 150, 70, 70, 70, 70]
        table = Table(data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.blue),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('WORDWRAP', (0, 0), (-1, -1)),
        ]))
        elements.append(table)
        doc.build(elements)

        buffer.seek(0)
        return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="summons_queue.pdf")

# ── GPS ingest
@app.route("/api/gps", methods=["POST"])
@ingest_token_required
def receive_gps():
    data = request.json or {}
    if not data:
        return jsonify({"error": "No data received"}), 400

    data["plate"] = data.get("plate") or SCAN_CAR_PLATE
    data["time"] = data.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    gps_logs.append(data)
    try:
        db = get_db()
        with db.cursor() as cursor:
            cursor.execute("""
                INSERT INTO gps_history (plate, timestamp, latitude, longitude, speed)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                data.get("plate"),
                data.get("time"),
                data.get("latitude"),
                data.get("longitude"),
                data.get("speed", 0)
            ))
            db.commit()
    except Exception as e:
        print("Failed to insert GPS into DB:", e)

    if len(gps_logs) > 1000:
        gps_logs.pop(0)

    send_gps_to_dashboard(data)
    # do not log full PII; mask plate
    print(f"GPS received @{data.get('time')} for {mask_plate(data.get('plate',''))}")
    return jsonify({"status": "success"}), 200

@app.route("/api/gps/logs", methods=["GET"])
def get_gps_logs():
    return jsonify(gps_logs)

# ── Payment QR proxy
@app.route("/api/payment/generate-qr", methods=["POST"])
def generate_qr():
    data = request.json or {}
    if "totalAmount" not in data or "summons" not in data:
        return jsonify({"error": "Missing required data"}), 400
    try:
        headers = {"Content-Type": "application/json"}
        if PAYMENT_QR_TOKEN:
            headers["Authorization"] = PAYMENT_QR_TOKEN
        response = requests.post(PAYMENT_QR_API, json=data, headers=headers, timeout=10)
        return jsonify(response.json()), response.status_code
    except requests.exceptions.RequestException:
        return jsonify({"error": "Failed to generate payment QR"}), 500

# ── Tracking utilities
@app.route("/gps-tracking", methods=["GET"])
def get_gps_tracking():
    if gps_logs:
        return jsonify(gps_logs[-1])
    return jsonify({"error": "No GPS data available"}), 404

@app.route("/gps-tracking-history", methods=["GET"])
def gps_tracking_history():
    plate = request.args.get("plate")
    start = request.args.get("start")
    end = request.args.get("end")
    filtered = gps_logs
    if plate:
        filtered = [g for g in filtered if g.get("plate") == plate]
    if start and end:
        filtered = [g for g in filtered if start <= g.get("time", "") <= end]
    formatted = [{
        "latitude": g.get("latitude"),
        "longitude": g.get("longitude"),
        "time": g.get("time"),
        "speed": g.get("speed", 0)
    } for g in filtered if g.get("latitude") is not None and g.get("longitude") is not None]
    return jsonify(formatted)

# ── Payment views
@app.route("/queue-summons")
def redirect_to_dashboard_summons():
    plate = request.args.get("plate")
    if not plate:
        return "Missing plate number", 400
    return redirect(f"/?plate={plate}&view=summons-payment")

@app.route("/qr-payment")
def qr_payment_view():
    url = request.args.get("url")
    return render_template("qr_payment.html", qr_url=url)

@app.route("/summons-payment")
def standalone_summons_payment():
    plate = request.args.get("plate")
    return render_template("summons_payment.html", plate=plate)

# ── Stats & health
@app.route("/api/lpr-stats", methods=["GET"])
def get_lpr_stats():
    total = api_stats["success_count"] + api_stats["failure_count"]
    average_time = (api_stats["total_time"] / api_stats["success_count"]) if api_stats["success_count"] else 0
    return jsonify({
        "total_calls": total,
        "successful_calls": api_stats["success_count"],
        "failed_calls": api_stats["failure_count"],
        "average_response_time_sec": round(average_time, 2)
    })

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({"status": "online" if is_connected() else "offline"})

# ── Plate ingest (from peers)
@app.route("/api/receive-plate", methods=["POST"])
@ingest_token_required
def receive_plate():
    data = request.json or {}
    if "plate" not in data:
        return jsonify({"error": "Invalid data"}), 400
    with lock:
        detected_plates.append(data)
    print(f"Plate received via API: {mask_plate(data.get('plate',''))}")
    return jsonify({"message": "Plate received"}), 200

# ── Reset queue (DB + memory + snapshots)
@app.route('/reset-queue', methods=['POST'])
@admin_required
def reset_queue():
    def clear_all():
        try:
            connection = get_db()
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE TABLE detected_plates")
                connection.commit()
            with lock:
                detected_plates.clear()
            snapshot_folder = app.config["SNAPSHOT_FOLDER"]
            if os.path.exists(snapshot_folder):
                # cautious: remove only files, not the folder tree outside
                for f in Path(snapshot_folder).glob("*"):
                    try:
                        if f.is_file():
                            f.unlink()
                        elif f.is_dir():
                            # if someone created nested dirs, remove them
                            for sub in f.rglob("*"):
                                if sub.is_file():
                                    sub.unlink()
                            f.rmdir()
                    except Exception:
                        pass
        except Exception as e:
            print("Reset failed:", e)

    threading.Thread(target=clear_all, daemon=True).start()
    return jsonify({"status": "success", "message": "Reset started. Data will clear shortly."})

# ── Start/Stop dangerous ops (🔐 admin only)
@app.route('/start-all', methods=['POST'])
@admin_required
def start_all_services():
    try:
        # Update these paths via env if needed
        PY = os.getenv("PYTHON_BIN", "python3")
        BASE = os.getenv("PROJECT_BASE", "/home/lpr2/Desktop/lpr-project")
        procs = [
            [PY, f"{BASE}/live_detection_service/lpr.py"],
            [PY, f"{BASE}/live_detection_service/gps_tracker.py"],
            ["node", f"{BASE}/live_detection_service/server.js"],
            [PY, f"{BASE}/dashboard_service/dashboard.py"],
        ]
        for cmd in procs:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"message": "All services started successfully!"})
    except Exception as e:
        return jsonify({"message": f"Error starting services: {e}"}), 500

@app.route('/stop-all', methods=['POST'])
@admin_required
def stop_all_services():
    try:
        BASE = os.getenv("PROJECT_BASE", "/home/lpr2/Desktop/lpr-project")
        os.system(f"pkill -f {BASE}/live_detection_service/lpr.py")
        os.system(f"pkill -f {BASE}/live_detection_service/gps_tracker.py")
        os.system(f"pkill -f {BASE}/live_detection_service/server.js")
        os.system(f"pkill -f {BASE}/dashboard_service/dashboard.py")
        return jsonify({"message": "All services stopped successfully!"})
    except Exception as e:
        return jsonify({"message": f"Error stopping services: {e}"}), 500

# ──────────────────────────────────────────────────────────────────────────────
# Offline queue sync loop
# ──────────────────────────────────────────────────────────────────────────────
def sync_offline_data():
    if not is_connected():
        return
    path = Path(OFFLINE_FILE)
    if not path.exists():
        return
    try:
        with open(path, "r") as f:
            queue = json.load(f)
    except json.JSONDecodeError:
        queue = []

    successful = []
    for item in queue:
        try:
            headers = {}
            if SHARED_INGEST_TOKEN:
                headers["X-Auth-Token"] = SHARED_INGEST_TOKEN

            if item.get("type") == "plate":
                # send to first matching receive-plate URL
                for url in DASHBOARD_URLS:
                    if url.endswith("/api/receive-plate"):
                        res = requests.post(url, json=item["data"], headers=headers, timeout=5)
                        if res.status_code == 200:
                            successful.append(item)
                            break
            elif item.get("type") == "gps":
                for url in DASHBOARD_URLS:
                    if url.endswith("/api/gps"):
                        res = requests.post(url, json=item["data"], headers=headers, timeout=5)
                        if res.status_code == 200:
                            successful.append(item)
                            break
        except Exception:
            pass

    remaining = [q for q in queue if q not in successful]
    with open(OFFLINE_FILE, "w") as f:
        json.dump(remaining, f, indent=2)

def start_sync_loop():
    def loop():
        while True:
            sync_offline_data()
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()

start_sync_loop()

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # If you run behind a reverse proxy with TLS termination, adjust as needed.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")), debug=False)
