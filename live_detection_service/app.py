from __future__ import annotations
from flask import Flask, request, jsonify, send_file, send_from_directory, url_for
import os, threading, cv2, numpy as np, requests, pandas as pd
from io import BytesIO
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

PLATE_RECOGNIZER_API_URL=os.getenv("PLATE_RECOGNIZER_API_URL","https://api.platerecognizer.com/v1/plate-reader/")
PLATE_RECOGNIZER_TOKEN=os.getenv("PLATE_RECOGNIZER_TOKEN","")
PARKING_API_URL=os.getenv("PARKING_API_URL","https://mycouncil.citycarpark.my/parking/ctcp/services-listerner_mbk.php")
PARKING_API_ACTION=os.getenv("PARKING_API_ACTION","GetParkingRightByPlateVerify")
PARKING_VERIFY_SSL=os.getenv("PARKING_VERIFY_SSL","true").lower() in ("1","true","yes","y","on")
SUMMONS_API_URL=os.getenv("SUMMONS_API_URL","http://localhost:5000/api/summons")
SNAPSHOT_DIR=os.getenv("SNAPSHOT_DIR", os.path.join(app.root_path,"static/snapshots"))
INGEST_TOKEN=os.getenv("SHARED_INGEST_TOKEN","")
PORT=int(os.getenv("PORT","5001"))

os.makedirs(SNAPSHOT_DIR, exist_ok=True)
detected_plates=[]; gps_data_list=[]; lock=threading.Lock()

def require_ingest_token():
    if not INGEST_TOKEN: return None
    return None if request.headers.get("X-Auth-Token","")==INGEST_TOKEN else (jsonify({"error":"invalid token"}), 401)

@app.route("/api/recognize-plate", methods=["POST"])
def recognize_plate():
    if 'image' not in request.files: return jsonify({"error":"No image file"}), 400
    if not PLATE_RECOGNIZER_TOKEN: return jsonify({"error":"Server not configured"}), 500
    f=request.files['image']; arr=np.frombuffer(f.read(), dtype=np.uint8); img=cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None: return jsonify({"error":"Invalid image"}), 400
    ok, enc=cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok: return jsonify({"error":"Encode failed"}), 500
    try:
        r=requests.post(PLATE_RECOGNIZER_API_URL,
                        files={"upload":("image.jpg", enc.tobytes(),"image/jpeg")},
                        headers={"Authorization": f"Token {PLATE_RECOGNIZER_TOKEN}"}, timeout=30)
        if r.status_code in (200,201): return jsonify(r.json().get("results",[])), 200
        return jsonify({"error":f"Plate API {r.status_code}", "body":r.text}), 502
    except requests.RequestException as e:
        return jsonify({"error":f"Plate API failed: {e}"}), 502

@app.route("/api/parking-status/<plate>", methods=["GET"])
def parking_status(plate):
    try:
        r=requests.get(PARKING_API_URL, params={"prpid":"","action":PARKING_API_ACTION,"filterid":plate},
                       verify=PARKING_VERIFY_SSL, timeout=15)
        if r.status_code==200: return jsonify(r.json()), 200
        return jsonify({"error":f"Parking API {r.status_code}"}), 502
    except requests.RequestException as e:
        return jsonify({"error":f"Parking API failed: {e}"}), 502

@app.route("/api/summons-status/<plate>", methods=["GET"])
def summons_status(plate):
    try:
        r=requests.post(SUMMONS_API_URL, json={"vehicleNumber": plate},
                        headers={"Content-Type":"application/json"}, timeout=15)
        if r.status_code in (200,201): return jsonify(r.json()), 200
        return jsonify({"error":f"Summons API {r.status_code}"}), 502
    except requests.RequestException as e:
        return jsonify({"error":f"Summons API failed: {e}"}), 502

@app.route("/plates", methods=["GET"])
def plates():
    with lock:
        payload=[]
        for p in detected_plates:
            snap=p.get("snapshot","")
            if snap and not snap.startswith("http"):
                snap=url_for("serve_snapshot", filename=os.path.basename(snap), _external=False)
            payload.append({**p, "snapshot": snap or ""})
        return jsonify(payload), 200

@app.route('/api/gps', methods=['POST'])
def receive_gps():
    bad=require_ingest_token(); 
    if bad: return bad
    data=request.json or {}
    if not data: return jsonify({"error":"No data"}), 400
    with lock:
        gps_data_list.append(data)
        if len(gps_data_list)>5000: gps_data_list[:]=gps_data_list[-2000:]
    return jsonify({"message":"GPS Data Received"}), 200

@app.route('/api/gps', methods=['GET'])
def get_gps():
    with lock: return jsonify(gps_data_list), 200

@app.route("/api/gps/logs", methods=["GET"])
def gps_logs():
    with lock: return jsonify(gps_data_list), 200

@app.route("/api/reports/detected-plates", methods=["GET"])
def report_detected():
    with lock:
        if not detected_plates: return jsonify({"error":"No detected plates available"}), 400
        df=pd.DataFrame(detected_plates)
    out=BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as w: df.to_excel(w, index=False, sheet_name="Detected Plates")
    out.seek(0)
    return send_file(out,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="detected_plates.xlsx")

@app.route('/static/snapshots/<path:filename>')
def serve_snapshot(filename):
    return send_from_directory(SNAPSHOT_DIR, filename)

if __name__=="__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
