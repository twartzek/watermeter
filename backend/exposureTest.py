"""
Test script for the Pi: takes an exposure series with the LED on, to find
out whether the reflections on the meter glass are overexposed (blown out)
or not.

Procedure:
  1. One image with auto exposure (as in readTotalConsumption.py) -- the
     ExposureTime/AnalogueGain chosen by auto exposure is printed.
  2. One image per fixed exposure time (auto exposure off, gain 1.0).

For each image the share of blown-out pixels (>= 250 in one channel) is
printed. If it drops clearly with shorter exposure and the needles/digits
are still clearly visible, a fixed, shorter ExposureTime is enough. If the
reflections stay visible as bright spots, only a polarizing
filter/diffuser/geometry will help.

Usage (on the Pi, NOT while a measurement is running via cron, since the
camera would be busy):
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
        f"clipped={clipped_percent(image_array):6.2f} %"
    )


def wait_for_exposure(picam2, target_us, max_frames=15):
    # New controls only take effect after a few frames -- discard frames
    # until the requested exposure time is applied.
    for _ in range(max_frames):
        exposure = picam2.capture_metadata().get("ExposureTime", 0)
        if abs(exposure - target_us) <= max(50, 0.05 * target_us):
            return
    print(f"  Warning: ExposureTime {target_us} us not reached (last {exposure} us)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exposures", type=int, nargs="+", default=[1000, 2000, 5000, 10000, 20000],
                        help="fixed exposure times in microseconds")
    parser.add_argument("--out", default=os.path.expanduser("~/exposure_test"),
                        help="output directory for the images")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    led = Led(2)
    picam2 = Picamera2()
    picam2.configure(picam2.create_still_configuration())

    led.on()
    try:
        picam2.start()
        time.sleep(1)  # as in readTotalConsumption.getImage(): let auto exposure settle
        capture(picam2, os.path.join(args.out, "auto.jpg"))

        for exposure in args.exposures:
            picam2.set_controls({"AeEnable": False, "ExposureTime": exposure, "AnalogueGain": 1.0})
            wait_for_exposure(picam2, exposure)
            capture(picam2, os.path.join(args.out, f"exp_{exposure:06d}us.jpg"))
    finally:
        led.off()
        picam2.close()

    print(f"\nImages saved in {args.out}")


if __name__ == "__main__":
    main()
