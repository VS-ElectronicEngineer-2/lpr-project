from flask import Flask, render_template, Response, jsonify, request, redirect, url_for, session, send_file
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

app = Flask(__name__)
app.secret_key = "supersecretkey"  # Change this to a secure key

# ✅ Dummy User Database (Replace with actual database in production)
USERS = {
    "admin": {"password": "password123", "officer_id": "111111"},
    "user1": {"password": "lprsystem", "officer_id": "222222"},
    "user2": {"password": "test123", "officer_id": "333333"}
}

app.config["SNAPSHOT_FOLDER"] = "static/snapshots"
if not os.path.exists(app.config["SNAPSHOT_FOLDER"]):
    os.makedirs(app.config["SNAPSHOT_FOLDER"])

# API Details
PLATE_RECOGNIZER_API_URL = "https://api.platerecognizer.com/v1/plate-reader/"
PARKING_API_URL = "https://mycouncil.citycarpark.my/parking/ctcp/services-listerner_mbk.php"
NODE_API_URL = "http://localhost:5000/api/summons"
API_TOKEN = "51a139644fb531a54a3e45f8e231427dac23b63e"
PARKING_API_ACTION = "GetParkingRightByPlateVerify"

detected_plates = []
summons_data = []  # Store fetched summons data globally
lock = threading.Lock()
frame_queue = Queue(maxsize=1)  # Increased queue size
gps_logs = []  # ✅ Store latest GPS readings
stored_officer_id = "Unknown"  # ✅ Store officer ID globally

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

throttler = Throttler(rate_limit=1, interval=1)  # 8 API calls per second

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

# Authentication Routes
@app.route("/login", methods=["GET", "POST"])
def login():
    global stored_officer_id
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        if username in USERS and USERS[username]["password"] == password:
            session["user"] = username
            session["officer_id"] = USERS[username]["officer_id"]
            stored_officer_id = USERS[username]["officer_id"]  # ✅ Store globally
            return redirect("/")
        else:
            return render_template("login.html", error="Invalid username or password.")

    return render_template("login.html")

@app.route("/logout")  # ✅ Ensure logout function is correctly defined
def logout():
    global stored_officer_id
    session.pop("user", None)
    session.pop("officer_id", None)
    stored_officer_id = "Unknown"  # ✅ Reset on logout
    return redirect("/login")

@app.route("/")
def dashboard():
    if "user" not in session:  # ✅ If user is not logged in, redirect to login page
        return redirect(url_for("login"))
    return render_template("index.html")  # ✅ If logged in, show the dashboard

# License Plate Recognition
def recognize_plate(frame):
    throttler.wait()

    try:
        _, img_encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50])  # ✅ Reduce quality for faster upload
        img_bytes = img_encoded.tobytes()  # ✅ Convert image to bytes

        print("📤 Sending image to Plate Recognizer API...")

        response = requests.post(
            PLATE_RECOGNIZER_API_URL,
            files={"upload": ("image.jpg", img_bytes, "image/jpeg")},  # ✅ Correct image upload
            headers={"Authorization": f"Token {API_TOKEN}"},
            timeout=30  # ✅ Increased timeout
        )

        print(f"📥 API Response Code: {response.status_code}")
        print(f"📥 API Response Data: {response.text}")

        if response.status_code == 201:
            return response.json().get("results", [])

        print("⚠️ Plate Recognizer API did not return a plate.")
        return []

    except requests.exceptions.RequestException as e:
        print(f"⚠️ Plate recognition request failed: {e}")
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

# Process frames asynchronously
def process_frames():
    global stored_officer_id  # ✅ Use the global officer ID

    while True:
        if not frame_queue.empty():
            frame = frame_queue.get()
            plates = recognize_plate(frame)

            for plate_data in plates:
                plate_number = plate_data.get("plate", "").upper()  # ✅ Ensure plate_number is assigned

                if not plate_number:  # ✅ Skip if no plate is detected
                    print("⚠️ No plate detected, skipping...")
                    continue  

                with lock:
                    if any(p["plate"] == plate_number for p in detected_plates):
                        continue  # ✅ Avoid duplicate entries

                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                snapshot_name = f"{plate_number}_{int(time.time())}.jpg"
                snapshot_path = os.path.join(app.config["SNAPSHOT_FOLDER"], snapshot_name)
                cv2.imwrite(snapshot_path, frame)

                # ✅ Get latest GPS data
                latitude, longitude = None, None
                if gps_logs:
                    latest_gps = gps_logs[-1]  # Get last received GPS log
                    latitude = latest_gps.get("latitude")
                    longitude = latest_gps.get("longitude")

                # ✅ Use global officer ID instead of session
                officer_id = stored_officer_id

                parking_status = check_parking_status(plate_number)
                summons_status = check_summons_status(plate_number)

                with lock:
                    detected_plates.append({
                        "plate": plate_number,  # ✅ This is now properly defined
                        "status": parking_status,
                        "summons": summons_status,
                        "time": timestamp,
                        "snapshot": snapshot_path,
                        "latitude": latitude,  # ✅ Include GPS data
                        "longitude": longitude,  # ✅ Include GPS data
                        "officer_id": officer_id  # ✅ Include Officer ID
                    })

    print(f"✅ Added Detected Plate: {detected_plates[-1]}")  # 🔍 Debugging log

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
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/plates", methods=["GET"])
def plates():
    with lock:
        for plate in detected_plates:
            if "officer_id" not in plate:
                plate["officer_id"] = stored_officer_id  # ✅ Ensure officer_id is present
        
        print(f"📤 Sending Detected Plates to Frontend: {detected_plates}")  # 🔍 Debugging log
        return jsonify(list(reversed(detected_plates)))  # Reverse order to show latest first

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
        gps_logs.append(data)  # ✅ Store received GPS data
        if len(gps_logs) > 10:  # ✅ Keep only last 10 entries to prevent memory overflow
            gps_logs.pop(0)  # Remove oldest entry

        print(f"📡 GPS Data Received: {data}")  # Debugging
        return jsonify({"status": "success"}), 200
    return jsonify({"error": "No data received"}), 400

@app.route("/api/gps/logs", methods=["GET"])
def get_gps_logs():
    return jsonify(gps_data_log)  # Return logged GPS data

@app.route("/api/payment/generate-qr", methods=["POST"])
def generate_qr():
    data = request.json
    if not data or "totalAmount" not in data:
        return jsonify({"error": "Missing totalAmount"}), 400
    
    total_amount = data["totalAmount"]
    
    # ✅ Simulate a QR Code URL (Replace with actual QR generation logic)
    qr_code_url = f"https://paymentgateway.com/pay?amount={total_amount}"
    
    return jsonify({"qrCode": qr_code_url})

@app.route("/gps-tracking", methods=["GET"])
def get_gps_tracking():
    if gps_logs:
        latest_gps = gps_logs[-1]  # ✅ Get last received GPS log
        return jsonify(latest_gps)
    return jsonify({"error": "No GPS data available"}), 404  # ✅ Return proper error message


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)










