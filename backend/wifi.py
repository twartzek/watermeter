import os
import subprocess
import time
from pyzbar.pyzbar import decode
import platform
import cv2
from time import sleep
import numpy as np
from mylog import Logger
from gpio import Led

logger = Logger("wifi")


try:
    from picamera2 import Picamera2
except ImportError:
    pass


led = Led(2)


def connect_to_wifi(ssid, password):

    logger.logger.info("Started connect_to_wifi")

    # Start Wi-Fi connection
    os.system(f"sudo nmcli dev wifi connect {ssid} password {password}")

    # Wait up to 15 seconds for a successful connection
    for _ in range(15):
        logger.logger.info("Checking connection...")
        result = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True)
        logger.logger.info(result.stdout)
        if ssid in result.stdout.strip():
            return True  # Connection successful
        time.sleep(1)  # Wait 1 second before checking again

    return False  # Timeout, connection failed


def capture_image():
    # Capture an image from the camera
    picam2 = Picamera2()
    camera_config = picam2.create_still_configuration()
    picam2.configure(camera_config)
    led.on()
    picam2.start()
    time.sleep(1)
    imageName = "wifi.jpg"
    filepath = os.path.join(os.getcwd(), imageName)
    picam2.capture_file(filepath)
    led.off()
    picam2.close()
    return filepath


def extract_wifi_credentials(image_path):
    '"  SSID:", wifi_credentials["S"]'
    '"  Password:", wifi_credentials["P"]'
    '"  Authentifizierung:", wifi_credentials["T"]'

    logger.logger.info("Started extract_wifi_credentials")
    # Open the image
    image =cv2.imread(image_path)
    if image.ndim == 3:
        image_gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else: # ndim == 2
        image_gray = image
    min_dim = min(image.shape[:2])
    min_block_size = int(min_dim/3)
    max_block_size = int(min_dim)


    step = int((max_block_size - min_block_size)/3)
    for block_size in range(min_block_size, max_block_size, step):
        logger.logger.debug("block size: " + str(block_size))
        led.blink()
        block_size += 0 if block_size%2 == 1 else 1 # blockSize should be odd
        image_bw = cv2.adaptiveThreshold(image_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, 2)

        decoded_objects = decode(image_bw)

        # Extract the WiFi credentials
        for obj in decoded_objects:
            if obj.type == "QRCODE":
                qr_code_data = obj.data.decode("utf-8")
                if qr_code_data.startswith("WIFI:"):
                    # Extract the WiFi credentials
                    wifi_credentials = {}
                    datastring = qr_code_data.lstrip("WIFI:").rstrip(";")
                    parts = datastring.split(";")
                    for part in parts:
                        key, value = part.split(":")
                        wifi_credentials[key] = value
                        logger.logger.debug("credentials extracted: " + key + " = " + value)
                    return wifi_credentials
                
    # Return none if no credentials were found            
    logger.logger.info("credentials not found")
    return None


def is_raspberry_pi():
    return (
        platform.machine() == "armv7l"
        or platform.machine() == "armv6l"
        or platform.machine() == "aarch64"
    )


def provide_user_feedback(status):
    if status == "success":
        led.fastblink(3)
        sleep(1) 
        led.off()
        logger.logger.info("Successfully connected to wifi")
    else:
        led.on()
        sleep(3)
        led.off()
        logger.logger.info("Not able to connect to wifi")


if __name__ == "__main__":

    led.off()

    wifi_credentials = None


    # Extract wifi credentials
    if is_raspberry_pi():
        imagePath = capture_image()
    else:
        # For testing purpose on your desktop
        # Change here if needed
        # imagePath = "wifi.jpg"
        logger.logger.warning("No camera detected. Exiting readTotalConsumption")
        exit()

    wifi_credentials = extract_wifi_credentials(imagePath)

    # If nothing was found, exit
    if wifi_credentials is None:
        provide_user_feedback("fail")
        exit()

    # Connect to wifi
    status = connect_to_wifi(wifi_credentials["S"], wifi_credentials["P"])

    # If connection failed, exit
    if status is False:
        provide_user_feedback("fail")
        exit()

    provide_user_feedback("success")



