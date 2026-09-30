#!/bin/bash
# Prevents overlapping runs: on the Pi Zero 2 W (416 MB RAM) a single
# measurement run can take much longer than the cron interval under memory
# pressure (see YOLO_PREDICT_TIMEOUT_SECONDS in readTotalConsumption.py).
# If cron then started a second run in parallel, the memory demand would
# double and make things even worse. flock skips the new run immediately
# if an old one is still running, instead of waiting or running in
# parallel.
LOCKFILE=/tmp/watermeter_startmeasurement.lock
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "Previous measurement still running. Skipping this run."
    exit 1
fi

SWAPSPACE=$(free -m | awk '/Swap/{print $4}' | tr -d '\n')
if [ $? -ne 0 ]; then
    echo "Error: failed to get swap space"
    exit 1
fi

if [ $SWAPSPACE -lt 1500 ]; then
    echo "Low swap space detected. Skipping measurement"
    exit 1
fi

# 20 minutes: readTotalConsumption.py makes up to two predict() calls
# (needles, then digits) one after the other, each with its own
# YOLO_PREDICT_TIMEOUT_SECONDS=550s budget there -- this outer timeout has
# to cover both together (plus image capture/overhead), otherwise it hits
# before the controlled inner timeout and we lose its clean
# cleanup/logging. ALWAYS adjust this consistently when changing
# YOLO_PREDICT_TIMEOUT_SECONDS there.
timeout 20m /home/admin/myvenv/bin/python /home/admin/watermeter/backend/readTotalConsumption.py

if [ $? -ne 0 ]; then
    echo "Error: failed to run python script"
    exit 1
fi

exit 0