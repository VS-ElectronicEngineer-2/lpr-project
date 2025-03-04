import gps
import time
import requests
import json
import numpy as np
from datetime import datetime

# Flask API endpoint to send GPS data
API_URL = "http://localhost:5001/api/gps"

# Movement tracking thresholds (in seconds and km/h)
IDLE_THRESHOLD = 60   # Time before considering the vehicle idle
END_THRESHOLD = 100   # Time before considering the trip ended
SPEED_THRESHOLD = 5   # Speed threshold (km/h) to determine movement

# Initialize tracking variables
start_time = None
idle_start_time = None
end_time = None
last_movement_time = None

# Local log file
LOG_FILE = "gps_log.json"

# Store last 5 GPS readings for smoothing
gps_data_buffer = []

def save_data_locally(data):
    """Save GPS data locally if API call fails."""
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
    """Apply moving average filter to GPS data to reduce jumps."""
    if len(data) < window_size:
        return np.mean(data)  # Return mean if not enough data points
    return np.convolve(data, np.ones(window_size)/window_size, mode='valid')[-1]

# Start GPS session
session = gps.gps(mode=gps.WATCH_ENABLE)
print("🚀 GPS Tracking Started... Waiting for movement.")

while True:
    try:
        report = session.next()
        if report['class'] == 'TPV':  
            speed = round(getattr(report, 'speed', 0) * 3.6, 2)  # Convert to km/h
            lat = getattr(report, 'lat', None)
            lon = getattr(report, 'lon', None)

            if lat is not None and lon is not None:
                # Store GPS data for smoothing
                gps_data_buffer.append((lat, lon))

                if len(gps_data_buffer) >= 5:
                    lat_values, lon_values = zip(*gps_data_buffer)
                    lat = smooth_gps_data(list(lat_values))
                    lon = smooth_gps_data(list(lon_values))
                    gps_data_buffer.pop(0)  # Keep buffer size small

                current_time = datetime.now()

                # Mark trip start
                if start_time is None:
                    start_time = current_time
                    print(f"🚗 Trip Started at {start_time}, Location: {lat}, {lon}")

                # If moving, update last movement time and reset idle timer
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

                    # Check if trip should end
                    if last_movement_time and (current_time - last_movement_time).total_seconds() >= END_THRESHOLD:
                        if end_time is None:
                            end_time = current_time
                            print(f"✅ Trip Ended at {end_time}, Total Duration: {end_time - start_time}")

                        # Save final log
                        final_data = {
                            "latitude": lat,
                            "longitude": lon,
                            "speed": speed,
                            "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                            "idle_time": idle_start_time.strftime("%Y-%m-%d %H:%M:%S") if idle_start_time else None,
                            "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S")
                        }

                        save_data_locally(final_data)

                        try:
                            response = requests.post(API_URL, json=final_data, timeout=5)
                            response.raise_for_status()
                        except requests.exceptions.RequestException:
                            print("⚠️ API failed, saving locally.")

                        break  # Stop tracking when trip ends

                # Send live GPS data
                data = {
                    "latitude": lat,
                    "longitude": lon,
                    "speed": speed,
                    "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "idle_time": idle_start_time.strftime("%Y-%m-%d %H:%M:%S") if idle_start_time else None,
                    "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S") if end_time else None
                }

                try:
                    response = requests.post(API_URL, json=data, timeout=5)
                    response.raise_for_status()
                except requests.exceptions.RequestException:
                    save_data_locally(data)

        time.sleep(1)  # Avoid overloading the system

    except KeyboardInterrupt:
        print("🚦 Tracking stopped by user.")
        break
    except Exception as e:
        print(f"❌ Error: {e}")






