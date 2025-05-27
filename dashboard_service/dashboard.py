from flask import Flask, jsonify, request, render_template
from datetime import datetime
import pymysql

app = Flask(__name__, static_url_path='/static')

# ✅ MySQL Connection
db = pymysql.connect(
    host="localhost",
    user="root",
    password="hananrazi",
    database="lpr_system"
)
cursor = db.cursor(pymysql.cursors.DictCursor)

# ========================
# 🔹 Web Pages
# ========================

@app.route("/")
def index():
    return render_template("dashboard.html")

# ========================
# 🔹 GPS API
# ========================

GPS_LOGS = []

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

# ========================
# 🔹 Plate API (Persistent)
# ========================

@app.route("/api/receive-plate", methods=["POST"])
def receive_plate():
    data = request.get_json()
    if data:
        if data.get("summons") and len(data["summons"]) > 0:
            data["status"] = "Scofflaw"

        snapshot = data.get("snapshot", "")
        if not snapshot.startswith("http"):
            data["snapshot"] = "static/default-car.png"

        try:
            print("📝 Inserting into dashboard_plates:", data)
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
            db.commit()
            print("✅ Plate saved.")
        except Exception as e:
            print("❌ Failed to insert into dashboard_plates:", e)

        return jsonify({"status": "success"}), 200

    return jsonify({"error": "No data received"}), 400

@app.route("/api/received-plates", methods=["GET"])
def get_received_plates():
    start = request.args.get("start")
    end = request.args.get("end")

    try:
        db.ping(reconnect=True)  # ✅ Fix for "Packet sequence" or idle timeout
        query = "SELECT * FROM dashboard_plates"
        params = []

        if start and end:
            query += " WHERE DATE(time) BETWEEN %s AND %s"
            params = [start, end]

        query += " ORDER BY id DESC"
        cursor.execute(query, params)
        rows = cursor.fetchall()

        plates = []
        for row in rows:
            time_value = row["time"]
            if isinstance(time_value, str):
                formatted_time = time_value
            else:
                formatted_time = time_value.strftime("%Y-%m-%d %H:%M:%S")

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
        print("❌ Error retrieving plates:", e)
        return jsonify({"error": str(e)}), 500

# ========================
# 🔹 Run Server
# ========================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False)
