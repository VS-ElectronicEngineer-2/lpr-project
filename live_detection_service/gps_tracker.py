import gps
import time
import requests
import json
import numpy as np
from datetime import datetime

# ✅ Send to both LPR and Dashboard
API_URLS = [
    "http://localhost:5001/api/gps",         # Raspberry Pi LPR backend
    "http://52.163.74.67:5002/api/gps"      # Azure/Dashboard server
]

# Movement tracking thresholds
IDLE_THRESHOLD = 60   # seconds
END_THRESHOLD = 1000  # seconds
SPEED_THRESHOLD = 10  # km/h

# Tracking state
start_time = None
idle_start_time = None
end_time = None
last_movement_time = None
gps_data_buffer = []  # Buffer for smoothing
LOG_FILE = "gps_log.json"  # Local fallback

def save_data_locally(data):
    try:
        try:
            with open(LOG_FILE, "r") as file:
                logs = json.load(file)
        except (json.JSONDecodeError, FileNotFoundError):
            logs = []

        logs.append(data)
        with open(LOG_FILE, "w") as file:
            json.dump(logs, file, indent=4)
    except Exception as e:
        print(f"⚠️ Failed to save data locally: {e}")

def smooth_gps_data(data, window_size=5):
    if len(data) < window_size:
        return np.mean(data)
    return np.convolve(data, np.ones(window_size)/window_size, mode='valid')[-1]

session = gps.gps(mode=gps.WATCH_ENABLE)
print("🚀 GPS Tracking Started... Waiting for movement.")

while True:
    try:
        report = session.next()
        if report['class'] == 'TPV':
            speed = round(getattr(report, 'speed', 0) * 3.6, 2)
            lat = getattr(report, 'lat', None)
            lon = getattr(report, 'lon', None)

            if lat is not None and lon is not None:
                gps_data_buffer.append((lat, lon))

                if len(gps_data_buffer) >= 5:
                    lat_values, lon_values = zip(*gps_data_buffer)
                    lat = smooth_gps_data(list(lat_values))
                    lon = smooth_gps_data(list(lon_values))
                    gps_data_buffer.pop(0)

                current_time = datetime.now()

                if start_time is None:
                    start_time = current_time
                    print(f"🚗 Trip Started at {start_time}, Location: {lat}, {lon}")

                if speed > SPEED_THRESHOLD:
                    last_movement_time = current_time
                    idle_start_time = None
                    print(f"🚗 Moving... Speed: {speed} km/h at {lat}, {lon}")
                else:
                    if idle_start_time is None:
                        idle_start_time = current_time

                    idle_duration = (current_time - idle_start_time).total_seconds()
                    if idle_duration >= IDLE_THRESHOLD:
                        print(f"🛑 Idle for {idle_duration:.0f} seconds at {lat}, {lon}")

                    if last_movement_time and (current_time - last_movement_time).total_seconds() >= END_THRESHOLD:
                        if end_time is None:
                            end_time = current_time
                            print(f"✅ Trip Ended at {end_time}, Duration: {end_time - start_time}")

                        final_data = {
                            "latitude": lat,
                            "longitude": lon,
                            "speed": speed,
                            "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                            "idle_time": idle_start_time.strftime("%Y-%m-%d %H:%M:%S") if idle_start_time else None,
                            "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S")
                        }

                        save_data_locally(final_data)

                        success = False
                        for url in API_URLS:
                            try:
                                response = requests.post(url, json=final_data, timeout=5)
                                response.raise_for_status()
                                print(f"📡 Sent FINAL GPS to {url}")
                                success = True
                            except requests.exceptions.RequestException:
                                print(f"⚠️ Final send failed to {url}")

                        if not success:
                            print("❌ All endpoints failed. Saved locally.")
                        break

                # 🔄 Send live GPS
                data = {
                    "latitude": lat,
                    "longitude": lon,
                    "speed": speed,
                    "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "idle_time": idle_start_time.strftime("%Y-%m-%d %H:%M:%S") if idle_start_time else None,
                    "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S") if end_time else None
                }

                success = False
                for url in API_URLS:
                    try:
                        response = requests.post(url, json=data, timeout=5)
                        response.raise_for_status()
                        print(f"📡 Sent live GPS to {url}")
                        success = True
                    except requests.exceptions.RequestException:
                        print(f"⚠️ Failed to send live GPS to {url}")

                if not success:
                    save_data_locally(data)

        time.sleep(1)

    except KeyboardInterrupt:
        print("🚦 Tracking stopped by user.")
        break
    except Exception as e:
        print(f"❌ Error: {e}")













