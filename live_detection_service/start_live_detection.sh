#!/bin/bash

# Run lpr.py in background
/usr/bin/python3 /home/lpr/Desktop/project/live_detection_service/lpr.py &

# Run gps_tracker.py in background
/usr/bin/python3 /home/lpr/Desktop/project/live_detection_service/gps_tracker.py &

# Run Node.js server
/usr/bin/node /home/lpr/Desktop/project/live_detection_service/server.js

