"""
Test-Skript fuer den Pi: nimmt bei eingeschalteter LED eine Belichtungsreihe
auf, um zu klaeren, ob die Reflexionen auf dem Zaehlerglas ueberbelichtet
(ausgebrannt) sind oder nicht.

Ablauf:
  1. Ein Bild mit Belichtungsautomatik (wie in readTotalConsumption.py) --
     die von der Automatik gewaehlte ExposureTime/AnalogueGain wird
     ausgegeben.
  2. Je ein Bild pro fester Belichtungszeit (Automatik aus, Gain 1.0).

Zu jedem Bild wird der Anteil ausgebrannter Pixel (>= 250 in einem Kanal)
ausgegeben. Sinkt dieser bei kuerzerer Belichtung deutlich und sind die
Zeiger/Ziffern dann noch gut erkennbar, reicht eine feste, kuerzere
ExposureTime. Bleiben die Reflexe als helle Flecken sichtbar, helfen nur
Polfilter/Diffusor/Geometrie.

Nutzung (auf dem Pi, NICHT waehrend eine Messung per Cron laeuft, da die
Kamera sonst belegt ist):
    cd backend
    python3 exposureTest.py
    python3 exposureTest.py --exposures 1000 2000 5000 10000 --out ~/exposure_test
"""

import argparse
import os
import time

import numpy as np
from PIL import Image
from picamera2 import Picamera2

from gpio import Led

CLIP_THRESHOLD = 250


def clipped_percent(image_array):
    return 100.0 * np.mean(image_array.max(axis=2) >= CLIP_THRESHOLD)


def capture(picam2, path):
    request = picam2.capture_request()
    try:
        image_array = request.make_array("main")
        metadata = request.get_metadata()
    finally:
        request.release()
    Image.fromarray(image_array).save(path)
    print(
        f"{os.path.basename(path):<24} "
        f"ExposureTime={metadata.get('ExposureTime'):>6} us  "
        f"AnalogueGain={metadata.get('AnalogueGain', 0):5.2f}  "
        f"Lux={metadata.get('Lux', 0):7.1f}  "
        f"ausgebrannt={clipped_percent(image_array):6.2f} %"
    )


def wait_for_exposure(picam2, target_us, max_frames=15):
    # Neue Controls greifen erst nach einigen Frames -- so lange Frames
    # verwerfen, bis die gewuenschte Belichtungszeit anliegt.
    for _ in range(max_frames):
        exposure = picam2.capture_metadata().get("ExposureTime", 0)
        if abs(exposure - target_us) <= max(50, 0.05 * target_us):
            return
    print(f"  Warnung: ExposureTime {target_us} us nicht erreicht (zuletzt {exposure} us)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exposures", type=int, nargs="+", default=[1000, 2000, 5000, 10000, 20000],
                        help="feste Belichtungszeiten in Mikrosekunden")
    parser.add_argument("--out", default=os.path.expanduser("~/exposure_test"),
                        help="Zielverzeichnis fuer die Bilder")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    led = Led(2)
    picam2 = Picamera2()
    picam2.configure(picam2.create_still_configuration())

    led.on()
    try:
        picam2.start()
        time.sleep(1)  # wie in readTotalConsumption.getImage(): AE einschwingen lassen
        capture(picam2, os.path.join(args.out, "auto.jpg"))

        for exposure in args.exposures:
            picam2.set_controls({"AeEnable": False, "ExposureTime": exposure, "AnalogueGain": 1.0})
            wait_for_exposure(picam2, exposure)
            capture(picam2, os.path.join(args.out, f"exp_{exposure:06d}us.jpg"))
    finally:
        led.off()
        picam2.close()

    print(f"\nBilder liegen in {args.out}")


if __name__ == "__main__":
    main()
