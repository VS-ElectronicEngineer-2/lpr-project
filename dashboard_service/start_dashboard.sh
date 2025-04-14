#!/bin/bash

# Start dashboard.py in background
python3 /home/lpr/Desktop/project/dashboard_service/dashboard.py &

# Wait for background processes to keep the script running
wait

