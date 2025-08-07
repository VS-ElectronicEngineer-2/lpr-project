from flask import Flask, render_template, Response, jsonify, request, redirect, url_for, session, send_file
import platform

if platform.system() == "Linux":
    from picamera2 import Picamera2

import cv2
import time
import threading
import os
import requests
from queue import Queue
import pandas as pd
from io import BytesIO
import gps
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle, SimpleDocTemplate, Paragraph, Image
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from datetime import datetime
import numpy as np  # Added for motion detection
import pymysql
from threading import Thread
import shutil
import socket
from pathlib import Path
import json
import subprocess
from threading import Thread

OFFLINE_FILE = "offline_queue.json"

def is_connected(host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False

def save_offline(data):
    path = Path(OFFLINE_FILE)
    if path.exists():
        with open(path, "r") as f:
            try:
                offline_data = json.load(f)
            except json.JSONDecodeError:
                offline_data = []
    else:
        offline_data = []

    offline_data.append(data)
    with open(path, "w") as f:
        json.dump(offline_data, f, indent=2)

# ✅ MariaDB connection
def get_db():
    return pymysql.connect(
        host="localhost",
        user="lpr_user",
        password="vistasummerose",
        database="lpr_system",
        cursorclass=pymysql.cursors.DictCursor  # Important for dict-style access
    )

app = Flask(__name__)
app.secret_key = "supersecretkey"  # Change this to a secure key

app.config["SNAPSHOT_FOLDER"] = "static/snapshots"
if not os.path.exists(app.config["SNAPSHOT_FOLDER"]):
    os.makedirs(app.config["SNAPSHOT_FOLDER"])

# API Details
PLATE_RECOGNIZER_API_URL = "https://api.platerecognizer.com/v1/plate-reader/"
PARKING_API_URL = "https://mycouncil.citycarpark.my/parking/ctcp/services-listerner_mbk.php"
NODE_API_URL = "http://localhost:5000/api/summons"
API_TOKEN = "7a5650fef8c594f93549eb9dea557d1bcbf1b42e"
PARKING_API_ACTION = "GetParkingRightByPlateVerify"

detected_plates = []
summons_data = []  # Store fetched summons data globally
lock = threading.Lock()
frame_queue = Queue(maxsize=1)  # Increased queue size
gps_logs = []  # ✅ Store latest GPS readings
stored_officer_id = "Unknown"  # ✅ Store officer ID globally

latest_gps = {"latitude": None, "longitude": None, "last_update": None}  # ✅ Global GPS storage

# API Logging Stats
api_stats = {
    "success_count": 0,
    "failure_count": 0,
    "total_time": 0.0
}

# API Throttler
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

throttler = Throttler(rate_limit=8, interval=1)  # 8 API calls per second

def crop_plate_region(frame):
    h, w, _ = frame.shape
    return frame[int(h * 0.1):int(h * 0.99), int(w * 0.01):int(w * 0.99)]

recent_plates = {}

def is_duplicate_plate(plate, cooldown=10):
    now = time.time()
    if plate in recent_plates and now - recent_plates[plate] < cooldown:
        return True
    recent_plates[plate] = now
    return False

def gps_updater():
    global latest_gps
    try:
        session = gps.gps(mode=gps.WATCH_ENABLE)
        for report in session:
            if report['class'] == 'TPV':
                lat = getattr(report, 'lat', None)
                lon = getattr(report, 'lon', None)
                if lat and lon:
                    latest_gps["latitude"] = round(lat, 6)
                    latest_gps["longitude"] = round(lon, 6)
                    latest_gps["last_update"] = time.time()
                    print(f"✅ GPS Updated: {latest_gps}")
    except Exception as e:
        print(f"❌ GPS updater error: {e}")

# Start GPS thread
threading.Thread(target=gps_updater, daemon=True).start()

# Initialize camera
try:
    picam2 = Picamera2()
    config = picam2.create_preview_configuration(main={"size": (640, 480), "format": "RGB888"})  # Lower resolution
    picam2.configure(config)
    picam2.start()
    print("Camera initialized successfully.")
except Exception as e:
    print(f"Camera initialization failed: {e}")
    picam2 = None

def send_gps_to_dashboard(data):
    if not is_connected():
        print("📴 No internet, saving GPS to offline queue")
        save_offline({"type": "gps", "data": data})
        return

    urls = [
        "http://52.163.74.67:5002/api/gps",
        "http://192.168.8.108:5002/api/gps"
    ]
    for url in urls:
        try:
            response = requests.post(url, json=data, timeout=5)
            if response.status_code == 200:
                print("📡 GPS forwarded successfully:", url)
                return
        except Exception as e:
            print("❌ Failed GPS upload:", e)

    print("📦 Saving GPS to offline queue (all URLs failed)")
    save_offline({"type": "gps", "data": data})

# Authentication Routes
@app.route("/login", methods=["GET", "POST"])
def login():
    global stored_officer_id

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
                user = cursor.fetchone()

                # ✅ DEBUG PRINTS (inside the try block)
                print(f"🧪 Trying login: username={username}, password={password}")
                print(f"🧪 DB user found: {user}")

        finally:
            conn.close()

        if user and password == user["password"]:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["officer_id"] = user["officer_id"]
            stored_officer_id = user["officer_id"]  # Store globally for detection
            return redirect(url_for("dashboard"))
        else:
            return render_template("login.html", error="Invalid login credentials.")

    return render_template("login.html")

@app.route("/logout")
def logout():
    global stored_officer_id
    session.clear()
    stored_officer_id = "Unknown"
    return redirect(url_for("login"))

@app.route("/")
def dashboard():
    if "user_id" not in session:  # ✅ If user is not logged in, redirect to login page
        return redirect(url_for("login"))
    return render_template("index.html")  # ✅ If logged in, show the dashboard

# License Plate Recognition
def recognize_plate(frame):
    throttler.wait()
    try:
        start_time = time.time()
        roi = crop_plate_region(frame)
        _, img_encoded = cv2.imencode(".jpg", roi, [int(cv2.IMWRITE_JPEG_QUALITY), 25])
        img_bytes = img_encoded.tobytes()

        print("📤 Sending image to Plate Recognizer API...")

        response = requests.post(
            PLATE_RECOGNIZER_API_URL,
            files={"upload": ("image.jpg", img_bytes, "image/jpeg")},
            headers={"Authorization": f"Token {API_TOKEN}"},
            timeout=30
        )

        elapsed = time.time() - start_time
        api_stats["total_time"] += elapsed

        if response.status_code == 201:
            api_stats["success_count"] += 1
            print(f"✅ Plate Recognizer Success in {elapsed:.2f}s | Total Success: {api_stats['success_count']}")
            return response.json().get("results", [])
        else:
            api_stats["failure_count"] += 1
            print(f"❌ API Error {response.status_code} in {elapsed:.2f}s | Total Failures: {api_stats['failure_count']}")
            return []

    except requests.exceptions.RequestException as e:
        api_stats["failure_count"] += 1
        print(f"❌ Request Exception: {e} | Total Failures: {api_stats['failure_count']}")
        return []

def check_parking_status(plate_number):
    try:
        response = requests.get(
            PARKING_API_URL,
            params={"prpid": "", "action": PARKING_API_ACTION, "filterid": plate_number},
            verify=False, timeout=5
        )
        if response.status_code == 200:
            result = response.json()
            if isinstance(result, list) and result:
                return f"Paid until {result[0].get('enddate', 'Unknown')} {result[0].get('endtime', '')}"
            return "Not Paid"
        return "Error"
    except requests.exceptions.RequestException as e:
        print(f"Parking API failed: {e}")
        return "Error"

def check_summons_status(plate_number):
    try:
        response = requests.post(
            NODE_API_URL,
            json={"vehicleNumber": plate_number},
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        data = response.json()
        if isinstance(data, list):  # ✅ If API returns a list, return it directly
            return data
        elif isinstance(data, dict) and "summonsQueue" in data:  # ✅ Handle dictionary response
            return data["summonsQueue"]
        return []  # ✅ Default return if format is unexpected
    except requests.exceptions.RequestException as e:
        print(f"Summons API failed: {e}")
        return []

# Frame processing

# ✅ Insert this updated section inside your `process_frames()` function

def process_frames():
    global stored_officer_id
    while True:
        if not frame_queue.empty():
            frame = frame_queue.get()
            plates = recognize_plate(frame)

            for plate_data in plates:
                plate_number = plate_data.get("plate", "").upper()

                if not plate_number:
                    print("⚠️ No plate detected, skipping...")
                    continue

                if is_duplicate_plate(plate_number):
                    print(f"⚠️ Recently detected {plate_number}, skipping duplicate.")
                    continue

                with lock:
                    if any(p["plate"] == plate_number for p in detected_plates):
                        continue

                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                snapshot_name = f"{plate_number}_{int(time.time())}.jpg"
                snapshot_path = os.path.join(app.config["SNAPSHOT_FOLDER"], snapshot_name)
                cv2.imwrite(snapshot_path, frame)

                # ✅ Use latest GPS from background thread
                latitude = latest_gps["latitude"]
                longitude = latest_gps["longitude"]

                if latitude is None or longitude is None:
                    print("⚠️ GPS still not ready, skipping this detection.")
                    continue
                
                # ✅ Avoid duplicate GPS coordinates for consecutive detections
                if detected_plates and detected_plates[-1]["latitude"] == latitude and detected_plates[-1]["longitude"] == longitude:
                    print("⚠️ Same GPS as last detection, skipping.")
                    continue

                officer_id = stored_officer_id

                # ✅ Check both parking and summons status
                parking_status = check_parking_status(plate_number)
                summons_status = check_summons_status(plate_number)

                # ✅ Smart status logic
                if summons_status and isinstance(summons_status, list) and len(summons_status) > 0:
                    final_status = summons_status[0].get("status", "Not Paid")
                elif "Paid until" in parking_status:
                    final_status = parking_status
                else:
                    final_status = "Not Paid"

                plate_info = {
                    "plate": plate_number,
                    "status": final_status,
                    "summons": summons_status,
                    "time": timestamp,
                    "snapshot": f"http://192.168.8.108:5001/static/snapshots/{snapshot_name}",
                    "latitude": latitude,
                    "longitude": longitude,
                    "officer_id": officer_id
                }

                with lock:
                    detected_plates.append(plate_info)
                    send_plate_to_dashboard(plate_info)
                print(f"✅ Added Detected Plate: {plate_info}")

                try:
                    # ✅ Insert into live view table
                    cursor.execute("""
                        INSERT INTO detected_plates (plate, timestamp, image_path, latitude, longitude, officer_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        plate_number,
                        timestamp,
                        snapshot_path,
                        latitude,
                        longitude,
                        officer_id
                    ))

                    # ✅ Insert into permanent history table
                    cursor.execute("""
                        INSERT INTO plate_history (plate, timestamp, image_path, latitude, longitude, officer_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        plate_number,
                        timestamp,
                        snapshot_path,
                        latitude,
                        longitude,
                        officer_id
                    ))

                    db.commit()
                    print("✅ Plate saved to DB")

                except Exception as e:
                    print("❌ Failed to insert plate into DB:", e)

# ✅ Helper to send data to dashboard

# ✅ Helper to send data to dashboard (Async/threaded version)
def send_plate_to_dashboard(plate_info):
    def forward():
        if not is_connected():
            print("📴 No internet, saving plate to offline queue")
            save_offline({"type": "plate", "data": plate_info})
            return

        dashboard_urls = [
            "http://192.168.8.108:5001/api/receive-plate",
            "http://192.168.8.108:5002/api/receive-plate",
            "http://52.163.74.67:5002/api/receive-plate",
            "http://52.163.74.67:5001/api/receive-plate"
        ]

        for url in dashboard_urls:
            try:
                print(f"🔁 Sending plate {plate_info['plate']} to {url}")
                response = requests.post(url, json=plate_info, timeout=5)
                print(f"📤 Response from {url}: {response.status_code}")
            except Exception as e:
                print(f"❌ Failed to send to {url}: {e}")

    Thread(target=forward).start()

threading.Thread(target=process_frames, daemon=True).start()

# Video feed generation with frame skipping
def generate_frames():
    if not picam2:
        yield b"Camera not initialized."
        return

    frame_skip = 1  # Process every nth frame
    count = 0

    while True:
        try:
            frame = picam2.capture_array()
            frame = cv2.resize(frame, (640, 480))  # Lower resolution
            count += 1

            # Add frame to queue only if it's the nth frame
            if count % frame_skip == 0 and not frame_queue.full():
                frame_queue.put(frame.copy())

            _, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])  # Lower quality
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")
        except Exception as e:
            print(f"Error capturing frame: {e}")
            yield b"Error capturing frame."
            break

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
        cursor.execute("""
            SELECT plate, timestamp, image_path, latitude, longitude, officer_id 
            FROM detected_plates ORDER BY id DESC LIMIT 100
        """)
        rows = cursor.fetchall()

        plates_from_db = []
        for row in rows:
            # ✅ Match status from in-memory detected_plates
            latest = next((p for p in detected_plates if p["plate"] == row[0]), None)
            status = latest["status"] if latest else "Not Paid"

            plate_data = {
                "plate": row[0],
                "status": status,  # ✅ Use real-time status from memory
                "time": row[1].strftime("%Y-%m-%d %H:%M:%S"),
                "snapshot": f"http://{request.host}/{row[2]}",
                "latitude": row[3],
                "longitude": row[4],
                "officer_id": row[5],
                "summons": latest["summons"] if latest else []
            }
            plates_from_db.append(plate_data)

        return jsonify(plates_from_db)
    except Exception as e:
        print("❌ Error loading plates from DB:", e)
        return jsonify([]), 500

@app.route("/api/user", methods=["GET"])
def get_user():
    global stored_officer_id
    if "user" in session:
        stored_officer_id = session["officer_id"]  # ✅ Store globally
        return jsonify({"user": session["user"], "officer_id": session["officer_id"]})
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
                    if summon["noticeNo"] not in unique_summons:
                        summon["latitude"] = plate["latitude"]
                        summon["longitude"] = plate["longitude"]
                        summon["snapshot"] = plate["snapshot"]
                        summon["officer_id"] = plate.get("officer_id", stored_officer_id)  
                        unique_summons[summon["noticeNo"]] = summon

    summons_data = list(unique_summons.values())  # Store summons globally
    
    print("📌 API Returning Summons Data:", summons_data)  # ✅ Debugging log
    return jsonify(summons_data)  # Reverse to show latest first

@app.route("/api/received-plates", methods=["GET"])
def get_received_plates():
    with lock:
        return jsonify(list(reversed(detected_plates)))

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

        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="detected_plates.xlsx")

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

        # Table Headers
        data = [["License Plate", "Status", "Time", "Snapshot"]]

        # Table Rows
        for plate in detected_plates:
            snapshot_path = plate["snapshot"]
            img = Image(snapshot_path, width=100, height=70)  # Adjusted image size
            
            # **Enable word wrapping for Status using Paragraph**
            status_text = Paragraph(plate["status"], styles["Normal"])
            
            data.append([
                plate["plate"],
                status_text,  # Apply word wrapping to the status column
                plate["time"],
                img
            ])

        # Increase column widths for better fit
        col_widths = [100, 120, 120, 120]

        # Create Table
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
            ('WORDWRAP', (0, 0), (-1, -1)),  # Enable word wrapping
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
        
        # Title with Centered Alignment
        styles = getSampleStyleSheet()
        title = Paragraph("<b>Summons Queue Report</b>", styles["Title"])
        elements.append(title)

        # Table Headers
        data = [["License Plate", "Notice No", "Offence", "Location", "Date", "Status", "Fine Amount", "Due Date"]]

        # Table Rows
        for summon in summons_data:
            data.append([
                summon["plate"],
                summon["noticeNo"],
                Paragraph(summon["offence"], styles["Normal"]),  # Wrap text properly
                Paragraph(summon["location"], styles["Normal"]), # Wrap text properly
                summon["date"],
                summon["status"],
                summon["amount"],
                summon["due_date"]
            ])

        # **Updated Column Widths**
        col_widths = [60, 90, 180, 150, 70, 70, 70, 70]  # Balanced layout for better fit

        # Create Table
        table = Table(data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.blue),  # Header background color
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),  # Header text color
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),  # Center align all text
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),  # Alternate row colors
            ('GRID', (0, 0), (-1, -1), 1, colors.black),  # Borders for all cells
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),  # Align vertically center
            ('FONTSIZE', (0, 0), (-1, -1), 9),  # Reduce font size for better fit
            ('WORDWRAP', (0, 0), (-1, -1)),  # Enable text wrapping for long content
        ]))

        elements.append(table)
        doc.build(elements)

        buffer.seek(0)

        return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="summons_queue.pdf")
        
gps_data_log = []  # Store GPS data temporarily

@app.route("/api/gps", methods=["POST"])
def receive_gps():
    global gps_logs
    data = request.json
    if data:
        # ✅ Inject fixed scan car plate and timestamp
        data["plate"] = "VMD9454"
        data["time"] = data.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        gps_logs.append(data)

        try:
            db = get_db()
            cursor = db.cursor()
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
            cursor.close()
            print("✅ GPS saved to DB")
        except Exception as e:
            print("❌ Failed to insert GPS into DB:", e)

        # ✅ This must be OUTSIDE the try block, no extra indent
        if len(gps_logs) > 1000:
            gps_logs.pop(0)

        send_gps_to_dashboard(data)
        print(f"📡 GPS Data Received: {data}")
        return jsonify({"status": "success"}), 200

    return jsonify({"error": "No data received"}), 400

@app.route("/api/gps/logs", methods=["GET"])
def get_gps_logs():
    return jsonify(gps_data_log)  # Return logged GPS data

@app.route("/api/payment/generate-qr", methods=["POST"])
def generate_qr():
    data = request.json
    if not data or "totalAmount" not in data or "summons" not in data:
        return jsonify({"error": "Missing required data"}), 400

    total_amount = data["totalAmount"]
    summons = data["summons"]

    try:
        response = requests.post(
            "http://localhost:5000/api/payment/generate-qr",  # ✅ Node.js API endpoint
            json={
                "totalAmount": total_amount,
                "summons": summons
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "2c76ee72a2e68a54e6e73ba360c6f1f41de42cb8c2235f645705ce1f834d7122"  # ✅ Replace with your actual token if needed
            },
            timeout=10
        )

        print("📥 Payment API Response:", response.text)
        return jsonify(response.json()), response.status_code

    except requests.exceptions.RequestException as e:
        print("❌ Payment request failed:", e)
        return jsonify({"error": "Failed to generate payment QR"}), 500


@app.route("/gps-tracking", methods=["GET"])
def get_gps_tracking():
    if gps_logs:
        latest_gps = gps_logs[-1]  # ✅ Get last received GPS log
        return jsonify(latest_gps)
    return jsonify({"error": "No GPS data available"}), 404  # ✅ Return proper error message

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
        "latitude": g["latitude"],
        "longitude": g["longitude"],
        "time": g["time"],
        "speed": g.get("speed", 0)
    } for g in filtered if "latitude" in g and "longitude" in g]

    return jsonify(formatted)  # ✅ Correctly indented now


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

@app.route("/api/lpr-stats", methods=["GET"])
def get_lpr_stats():
    total = api_stats["success_count"] + api_stats["failure_count"]
    average_time = (
        api_stats["total_time"] / api_stats["success_count"]
        if api_stats["success_count"] > 0 else 0
    )
    return jsonify({
        "total_calls": total,
        "successful_calls": api_stats["success_count"],
        "failed_calls": api_stats["failure_count"],
        "average_response_time_sec": round(average_time, 2)
    })

@app.route('/reset-queue', methods=['POST'])
def reset_queue():
    def clear_all():
        global detected_plates
        try:
            # 1. Truncate DB table (faster than DELETE)
            connection = pymysql.connect(
                host='localhost',
                user='lpr_user',
                password='vistasummerose',
                database='lpr_system',
                cursorclass=pymysql.cursors.DictCursor
            )
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("TRUNCATE TABLE detected_plates")
                    connection.commit()

            # 2. Clear in-memory queue
            with lock:
                detected_plates.clear()

            # 3. Delete and recreate snapshots folder (faster cleanup)
            snapshot_folder = app.config["SNAPSHOT_FOLDER"]
            if os.path.exists(snapshot_folder):
                shutil.rmtree(snapshot_folder)
            os.makedirs(snapshot_folder)

            print("✅ Reset complete: DB + memory + snapshots cleared.")

        except Exception as e:
            print("❌ Reset failed:", e)

    # Run the clear operation in background so user gets instant response
    Thread(target=clear_all).start()

    return jsonify({"status": "success", "message": "Reset started. Data will clear shortly."})

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({"status": "online" if is_connected() else "offline"})

@app.route("/api/receive-plate", methods=["POST"])
def receive_plate():
    data = request.json
    if not data or "plate" not in data:
        return jsonify({"error": "Invalid data"}), 400

    with lock:
        detected_plates.append(data)
    print(f"📥 Plate received via API: {data}")
    return jsonify({"message": "Plate received"}), 200

def sync_offline_data():
    if not is_connected():
        return

    path = Path(OFFLINE_FILE)
    if not path.exists():
        return

    with open(path, "r") as f:
        try:
            queue = json.load(f)
        except json.JSONDecodeError:
            queue = []

    successful = []
    for item in queue:
        try:
            if item["type"] == "plate":
                res = requests.post("http://52.163.74.67:5002/api/receive-plate", json=item["data"], timeout=5)
            elif item["type"] == "gps":
                res = requests.post("http://52.163.74.67:5002/api/gps", json=item["data"], timeout=5)
            else:
                continue

            if res.status_code == 200:
                successful.append(item)
        except:
            continue

    remaining = [q for q in queue if q not in successful]
    with open(OFFLINE_FILE, "w") as f:
        json.dump(remaining, f, indent=2)

def start_sync_loop():
    def loop():
        while True:
            sync_offline_data()
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()

# Start offline sync loop
start_sync_loop()

@app.route('/start-all', methods=['POST'])
def start_all_services():
    try:
        subprocess.Popen(['python3', '/home/lpr2/Desktop/lpr-project/live_detection_service/lpr.py'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(['python3', '/home/lpr2/Desktop/lpr-project/live_detection_service/gps_tracker.py'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(['node', '/home/lpr2/Desktop/lpr-project/live_detection_service/server.js'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(['python3', '/home/lpr2/Desktop/lpr-project/dashboard_service/dashboard.py'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"message": "✅ All services started successfully!"})
    except Exception as e:
        return jsonify({"message": f"❌ Error starting services: {e}"}), 500

@app.route('/stop-all', methods=['POST'])
def stop_all_services():
    try:
        os.system("pkill -f /home/lpr2/Desktop/lpr-project/live_detection_service/lpr.py")
        os.system("pkill -f /home/lpr2/Desktop/lpr-project/live_detection_service/gps_tracker.py")
        os.system("pkill -f /home/lpr2/Desktop/lpr-project/live_detection_service/server.js")
        os.system("pkill -f /home/lpr2/Desktop/lpr-project/dashboard_service/dashboard.py")
        return jsonify({"message": "✅ All services stopped successfully!"})
    except Exception as e:
        return jsonify({"message": f"❌ Error stopping services: {e}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)