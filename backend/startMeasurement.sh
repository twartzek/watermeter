#!/bin/bash
# Verhindert ueberlappende Laeufe: auf dem Pi Zero 2 W (416 MB RAM) kann ein
# einzelner Messlauf unter Speicherdruck deutlich laenger als die
# 10-Minuten-Cron-Periode dauern (siehe YOLO_PREDICT_TIMEOUT_SECONDS in
# readTotalConsumption.py). Wuerde Cron dann einen zweiten Lauf parallel
# starten, verdoppelt sich der Speicherbedarf und die Situation
# verschlimmert sich weiter. flock ueberspringt den neuen Lauf sofort,
# wenn noch ein alter laeuft, statt zu warten oder parallel loszulaufen.
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

# 20 Minuten: readTotalConsumption.py macht bis zu zwei predict()-Aufrufe
# (Needles, dann Digits) nacheinander, jeder mit eigenem
# YOLO_PREDICT_TIMEOUT_SECONDS=550s-Budget dort -- dieses aeussere Timeout
# muss beide zusammen (plus Bildaufnahme/Overhead) abdecken koennen, sonst
# schlaegt es vor dem kontrollierten inneren Timeout zu und wir verlieren
# dessen sauberes Cleanup/Logging. Bei einer Aenderung von
# YOLO_PREDICT_TIMEOUT_SECONDS dort IMMER hier konsistent mit anpassen.
timeout 20m /home/admin/myvenv/bin/python /home/admin/watermeter/backend/readTotalConsumption.py

if [ $? -ne 0 ]; then
    echo "Error: failed to run python script"
    exit 1
fi

exit 0