from flask import Flask, jsonify, request, render_template
from datetime import datetime
import os

app = Flask(__name__, static_url_path='/static')  # ✅ Enable static file support

# GPS logs and playback data
GPS_LOGS = []

@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/gps-tracking")
def gps_tracking():
    if GPS_LOGS:
        return jsonify(GPS_LOGS[-1])
    return jsonify({"error": "No GPS data"}), 404

@app.route("/gps-tracking-history")
def gps_tracking_history():
    plate = request.args.get("plate")
    start = request.args.get("start")
    end = request.args.get("end")

    filtered = [log for log in GPS_LOGS if log.get("plate") == plate]
    if start and end:
        filtered = [g for g in filtered if start <= g.get("time", "") <= end]

    return jsonify(filtered)

@app.route("/api/gps", methods=["POST"])
def receive_gps():
    global GPS_LOGS
    data = request.json
    if data:
        data["plate"] = "VMD9454"
        data["time"] = data.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        GPS_LOGS.append(data)

        if len(GPS_LOGS) > 1000:
            GPS_LOGS = GPS_LOGS[-1000:]

        print("📍 GPS_LOGS updated:", data)
        return jsonify({"status": "received"})
    return jsonify({"error": "no data"}), 400

# Detected plates storage
received_plates = []

@app.route("/api/receive-plate", methods=["POST"])
def receive_plate():
    data = request.get_json()
    if data:
        if data.get("summons") and len(data["summons"]) > 0:
            data["status"] = "Scofflaw"

        snapshot = data.get("snapshot", "")
        if not snapshot.startswith("http"):
            # If snapshot is not a full URL, fall back to default image
            data["snapshot"] = "static/default-car.png"

        received_plates.append(data)
        print(f"📥 Received plate: {data.get('plate')} | Snapshot: {data.get('snapshot')}")
        return jsonify({"status": "success"}), 200

    return jsonify({"error": "No data received"}), 400

from datetime import datetime

@app.route("/api/received-plates", methods=["GET"])
def get_received_plates():
    start = request.args.get("start")
    end = request.args.get("end")

    def is_within_range(plate):
        try:
            if not plate.get("time"):
                return False
            plate_date = datetime.strptime(plate["time"], "%Y-%m-%d %H:%M:%S").date()
            if start and end:
                start_date = datetime.strptime(start, "%Y-%m-%d").date()
                end_date = datetime.strptime(end, "%Y-%m-%d").date()
                return start_date <= plate_date <= end_date
            return True
        except Exception as e:
            print("❌ Date parsing error:", e)
            return False

    filtered = [p for p in received_plates if is_within_range(p)]
    return jsonify(filtered[::-1])  # Show latest first

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False)
