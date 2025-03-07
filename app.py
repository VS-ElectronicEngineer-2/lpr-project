from flask import Flask, request, jsonify, send_file
import os
from picamera2 import Picamera2
import cv2
import requests
import pandas as pd
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

app = Flask(__name__)

# API Details (for Plate Recognizer, Parking, Summons APIs)
PLATE_RECOGNIZER_API_URL = "https://api.platerecognizer.com/v1/plate-reader/"
PARKING_API_URL = "https://mycouncil.citycarpark.my/parking/ctcp/services-listerner_mbk.php"
SUMMONS_API_URL = "http://localhost:5000/api/summons"
API_TOKEN = "18cc09bdb0d72b43759a67ad9984a81ad2d153f0"
PARKING_API_ACTION = "GetParkingRightByPlateVerify"

detected_plates = []  # Store detected plates and parking/summons info

# Route for License Plate Recognition
@app.route("/api/recognize-plate", methods=["POST"])
def recognize_plate():
    if 'image' not in request.files:
        return jsonify({"error": "No image file"}), 400
    
    image_file = request.files['image']
    _, img_encoded = cv2.imencode('.jpg', image_file.read())
    
    # Send to Plate Recognizer API
    response = requests.post(
        PLATE_RECOGNIZER_API_URL,
        files={"upload": ("image.jpg", img_encoded.tobytes(), "image/jpeg")},
        headers={"Authorization": f"Token {API_TOKEN}"}
    )
    
    if response.status_code == 201:
        plate_data = response.json().get("results", [])
        return jsonify(plate_data), 200
    return jsonify({"error": "Failed to recognize plate"}), 500

# Route for Parking Status
@app.route("/api/parking-status/<plate_number>", methods=["GET"])
def parking_status(plate_number):
    try:
        response = requests.get(
            PARKING_API_URL,
            params={"prpid": "", "action": PARKING_API_ACTION, "filterid": plate_number},
            verify=False
        )
        if response.status_code == 200:
            parking_info = response.json()
            return jsonify(parking_info), 200
        return jsonify({"error": "Failed to fetch parking status"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Route for Summons Status
@app.route("/api/summons-status/<plate_number>", methods=["GET"])
def summons_status(plate_number):
    try:
        response = requests.post(
            SUMMONS_API_URL,
            json={"vehicleNumber": plate_number},
            headers={"Content-Type": "application/json"}
        )
        if response.status_code == 200:
            summons_info = response.json()
            return jsonify(summons_info), 200
        return jsonify({"error": "Failed to fetch summons status"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Route for receiving GPS data
@app.route("/api/gps", methods=["POST"])
def receive_gps():
    data = request.json
    detected_plates.append(data)  # Save received GPS data for later
    return jsonify({"status": "success"}), 200

# Route for GPS logs
@app.route("/api/gps/logs", methods=["GET"])
def get_gps_logs():
    return jsonify(detected_plates), 200

# Route for Reports
@app.route("/api/reports/detected-plates", methods=["GET"])
def generate_report():
    # Generate Excel/PDF report for detected plates
    if detected_plates:
        df = pd.DataFrame(detected_plates)
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False)
        output.seek(0)
        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="detected_plates.xlsx")
    
    return jsonify({"error": "No detected plates available"}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
