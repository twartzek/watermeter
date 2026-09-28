#!/bin/bash

# Increase swap memory
echo "Increasing swap memory..."
dphys-swapfile swapoff
echo "CONF_SWAPSIZE=2048" > /etc/dphys-swapfile
dphys-swapfile setup
dphys-swapfile swapon

# Enable wifi powersave. Disabling it was tried first to improve
# reliability, but on the Pi Zero 2 W's BCM43430/1 chip it instead caused
# frequent brcmfmac driver hangs ("brcmf_sdio_bus_rxctl: resumed on
# timeout", "GET_ASSOCLIST failed, err=-110") that made the Pi drop off
# the network entirely, triggering the watchdog's hard reboot (see
# /etc/watchdog.conf, ping target 8.8.8.8) several times a night.
sudo nmcli con mod preconfigured wifi.powersave enable

# Install system dependencies
echo "Installing dependencies..."
apt-get update
apt-get upgrade -y
apt install -y python3-picamera2 --no-install-recommends
apt install python3-pip -y
apt -y install zbar-tools
apt install npm
npm install --global serve
apt-get install watchdog

#  Install python dependencies
echo "Installing python dependencies..."
python3 -m venv --system-site-packages myvenv
source /home/admin/myvenv/bin/activate
pip install ultralytics
pip install opencv-python-headless
pip install matplotlib
pip install peewee
pip install scipy
pip install simplejpeg --ignore-installed
pip install python-crontab
pip install fastapi
pip install uvicorn
pip install python-dotenv
pip install pyzbar
pip install paho-mqtt
pip install scikit-learn
pip install pytest

# Create directories
sudo -u admin mkdir -p /home/admin/log
sudo -u admin mkdir -p /home/admin/data
sudo -u admin mkdir -p /home/admin/data/images
sudo -u admin mkdir -p /home/admin/watermeter/frontend/dist
sudo -u admin mkdir -p /home/admin/watermeter/backend

# Create .env file
echo "Creating .env file..."
sudo -u admin cat > /home/admin/watermeter/.env << EOF
db=/home/admin/data/watermeter.db
images=/home/admin/data/images
log=/home/admin/log
venv=/home/admin/myvenv
mainpath=/home/admin/watermeter
model_digits=/home/admin/watermeter/backend/models/digits/best.pt
model_needles=/home/admin/watermeter/backend/models/totalandneedles/best.pt
settingspath=/home/admin/data/settings.json
frontend=/home/admin/watermeter/frontend/dist
EOF



# Create systemd service for wifi setup at boot
echo "Creating systemd service for wifi setup at boot..."
# cat > /etc/systemd/system/watermeter_wifi.service > /dev/null <<EOF
# [Unit]
# Description=Watermeter WiFi Setup Service
# After=network.target    

# [Service]
# ExecStart=/home/admin/watermeter/wifi.py
# WorkingDirectory=/home/admin/

# [Install]
# WantedBy=multi-user.target
# EOF

echo "Enabling and starting wifi config and startup.."
# systemctl daemon-reload
# systemctl enable watermeter_wifi
# systemctl start watermeter_wifi


# Create systemd service for api server
echo "Creating systemd service for the rest api server..."
cat > /etc/systemd/system/watermeter_restapi.service <<EOF
[Unit]
Description=Watermeter Rest API Service
After=network.target

[Service]
ExecStart=/home/admin/myvenv/bin/python /home/admin/watermeter/backend/restapi.py
WorkingDirectory=/home/admin/
User=admin
Group=admin
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF


echo "Enabling and starting rest api server.."
systemctl daemon-reload
systemctl enable watermeter_restapi
systemctl start watermeter_restapi


# Create systemd service for frontend server
echo "Creating systemd service for the frontend server..."
cat > /etc/systemd/system/watermeter_frontend.service  <<EOF
[Unit]
Description=Watermeter Frontend Server Service
After=network.target

[Service]
ExecStart=npx serve --cors --single /home/admin/watermeter/frontend/dist -p 80
WorkingDirectory=/home/admin/
User=root
Group=root
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "Enabling and starting frontend server.."
systemctl daemon-reload
systemctl enable watermeter_frontend
systemctl start watermeter_frontend

# Create Start Measurement Script
#
# Gehalten wie backend/startMeasurement.sh im Repo (siehe dort fuer die
# Begruendung von flock/Timeout) -- bei Aenderungen an einer der beiden immer
# auch die andere anpassen, sonst driften Neuinstallationen (dieses Skript)
# und bestehende Installationen (die eigenstaendige Datei) auseinander.
echo "Creating start measurement script..."
cat > /home/admin/watermeter/backend/startMeasurement.sh << 'EOF'
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
EOF
chmod +x /home/admin/watermeter/backend/startMeasurement.sh





# Config gpio output
echo "Configuring gpio pins..."
echo "gpio=2=op,pd" >> /boot/firmware/config.txt
pinctrl 2 op pd dl


# Reduce swapiness
echo "Reduce swapiness..."
echo "vm.swappiness=10" >> /etc/sysctl.conf

# Setup crontab job
sudo crontab -u root -e << EOF
@reboot /usr/sbin/iw wlan0 set power_save off > /home/admin/power_save_log.txt 2>&1
@reboot /home/admin/myvenv/bin/python ~/watermeter/backend/wifi.py
EOF

# Create watchdog config
echo "Creating watchdog config..."
cat > /etc/watchdog.conf << EOF
# ====================================================================
# Configuration for the watchdog daemon. For more information on the
# parameters in this file use the command 'man watchdog.conf'
# ====================================================================

# =================== The hardware timer settings ====================
#
# For this daemon to be effective it really needs some hardware timer
# to back up any reboot actions. If you have a server then see if it
# has IPMI support. Otherwise for Intel-based machines try the iTCO_wdt
# module, otherwise (or if that fails) then see if any of the following
# module load and work:
#
# it87_wdt it8712f_wdt w83627hf_wdt w83877f_wdt w83977f_wdt
#
# If all else fails then 'softdog' is better than no timer at all!
# Or work your way through the modules listed under:
#
# /lib/modules/`uname -r`/kernel/drivers/watchdog/
#
# To see if they load, present /dev/watchdog, and are capable of
# resetting the system on time-out.

# Uncomment this to use the watchdog device driver access "file".

watchdog-device		= /dev/watchdog

# Uncomment and edit this line for hardware timeout values that differ
# from the default of one minute.
#
# Auf dem Pi Zero 2 W (416 MB RAM) kam es unter Speicherdruck (YOLO-
# Doppelinferenz in readTotalConsumption.py) zu einem Haenger, bei dem
# weder der ping- noch der load-Check griff und der Pi >20 Minuten
# unresponsive blieb, bis ein manueller Power-Reset noetig war --
# vermutlich weil der watchdog-Daemon selbst unter Swap-Thrashing keine
# rechtzeitige CPU-/IO-Zeit mehr bekam. 120s (statt der urspruenglichen
# 1000s) reagiert deutlich schneller auf einen echten Haenger, auf Kosten
# haeufigerer Reboots bei kurzen Lastspitzen.

watchdog-timeout	= 120

# If your watchdog trips by itself when the first timeout interval
# elapses then try uncommenting the line below and changing the
# value to 'yes'.

#watchdog-refresh-use-settimeout	= auto

# If you have a buggy watchdog device (e.g. some IPMI implementations)
# try uncommenting this line and setting it to 'yes'.

#watchdog-refresh-ignore-errors	= no

# ====================== Other system settings ========================
#
# Interval between tests. Should be a couple of seconds shorter than
# the hardware time-out value.
#
# Passend zu watchdog-timeout=120 oben verkuerzt (sollte laut watchdog
# selbst deutlich unter der Haelfte des Timeouts liegen, sonst Warnung
# "should be more than double interval" im Log).

interval		= 30

# The number of intervals skipped before a log message is written (i.e.
# a multiplier for 'interval' in terms of syslog messages)

logtick        = 1

# Directory for log files (probably best not to change this)

log-dir		= /var/log/watchdog

# Email address for sending the reboot reason. This needs sendmail to
# be installed and properly configured. Maybe you should just enable
# syslog forwarding instead?

#admin			= root

# Lock the daemon in to memory as a real-time process. This greatly
# decreases the chance that watchdog won't be scheduled before your
# machine is really loaded.

realtime		= yes
priority		= 1

# ====================== How to handle errors  =======================
#
# If you have a custom binary/script to handle errors then uncomment
# this line and provide the path. For 'v1' test binary files they also
# handle error cases.

#repair-binary		= /usr/sbin/repair
#repair-timeout		= 60

# The retry-timeout and repair limit are used to handle errors in a
# more robust manner. Errors must persist for longer than this to
# action a repair or reboot, and if repair-maximum attempts are
# made without the test passing a reboot is initiated anyway.

#retry-timeout		= 60
#repair-maximum		= 1

# Configure the delay on reboot from sending SIGTERM to all processes
# and to following up with SIGKILL for any that are ignoring the polite
# request to stop.

#sigterm-delay		= 5

# ====================== User-specified tests ========================
#
# Specify the directory for auto-added 'v1' test programs (any executable
# found in the 'test-directory should be listed).

#test-directory = /etc/watchdog.d

# Specify any v0 custom tests here. Multiple lines are permitted, but
# having any 'v1' programs/scripts discovered in the 'test-directory' is
# the better way.

#test-binary		=

# Specify the time-out value for a test error to be reported.

#test-timeout		= 60

# ====================== Typical tests ===============================
#
# Specify any IPv4 numeric addresses to be probed.
# NOTE: You should check you have permission to ping any machine before
# using it as a test. Also remember if the target goes down then this
# machine will reboot as a result!

ping			= 8.8.8.8
#ping			= 192.168.1.1

# Set the number of ping attempts in each 'interval' of time. Default
# is 3 and it completes on the first successful ping.
# NOTE: Round-trip delay has to be less than 'interval' / 'ping-count'
# for test success, but this is unlikely to be exceeded except possibly
# on satellite links (very unlikely case!).

#ping-count		= 3

# Specify any network interface to be checked for activity.

#interface		= wlan0

# Specify any files to be checked for presence, and if desired, checked
# that they have been updated more recently than 'change' seconds.
#
# readTotalConsumption.log (not db.log) on purpose: db.log is only written
# on a SUCCESSFUL detection (store_reading() in db.py), so several
# consecutive failed-but-not-hung readings (e.g. bad lighting overnight)
# would leave it stale for >900s and trigger a reboot even though nothing
# is actually hung. readTotalConsumption.log is touched at the START of
# every single cron run regardless of outcome, so it only goes stale when a
# run genuinely never starts or never finishes -- the actual hang case this
# check exists for.

file			= /home/admin/log/readTotalConsumption.log
change			= 900

# Uncomment to enable load average tests for 1, 5 and 15 minute
# averages. Setting one of these values to '0' disables it. These
# values will hopefully never reboot your machine during normal use
# (if your machine is really hung, the loadavg will go much higher
# than 25 in most cases).

max-load-1		= 10
max-load-5		= 8
max-load-15		= 5

# Check available memory on the machine.
#
# The min-memory check is a passive test from reading the file
# /proc/meminfo and computed from MemFree + Buffers + Cached
# If this is below a few tens of MB you are likely to have problems.
#
# The allocatable-memory is an active test checking it can be paged
# in to use.
#
# Maximum swap should be based on normal use, probably a large part of
# available swap but paging 1GB of swap can take tens of seconds.
#
# NOTE: This is the number of pages, to get the real size, check how
# large the pagesize is on your machine (typically 4kB for x86 hardware).

# ACHTUNG: min-memory=2560 (rechnerisch 10 MiB bei 4kB Pages, siehe
# getconf PAGESIZE) wurde ausprobiert, loeste aber bereits bei 141 MiB
# verfuegbarem Speicher aus ("memory available 144872 kB is less than
# 2560 pages" im Log) -- deutlich zu frueh, praktisch bei jedem
# YOLO-Messlauf. Offenbar interpretiert dieses watchdog-Binary (5.16-2)
# den min-memory-Wert nicht wie in `man watchdog.conf` beschrieben.
# Vor einer Reaktivierung den tatsaechlichen Ausloeseschwellenwert live
# ermitteln, statt aus der Page-Rechnung abzuleiten.
#min-memory		= 2560
#allocatable-memory	= 1
#max-swap = 0

# Check for over-temperature. Typically the temperature-sensor is a
# 'virtual file' under /sys and it contains the temperature in
# milli-Celsius. Usually these are generated by the 'sensors' package,
# but take care as device enumeration may not be fixed.

#temperature-sensor	=
#max-temperature	= 90

# Check for a running process/daemon by its PID file. For example,
# check if rsyslogd is still running by enabling the following line:

#pidfile		= /var/run/rsyslogd.pid
EOF



# debug run detection: /home/admin/myvenv/bin/python ~/watermeter/backend/readTotalConsumption.py 

