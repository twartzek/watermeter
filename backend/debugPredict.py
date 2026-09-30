"""
Debug script to run a single photo locally (desktop) through EXACTLY the
same YOLO detection pipeline that runs on the Pi in readTotalConsumption.py
(same preprocessing, same models, same predict() parameters) -- just
without camera/DB/MQTT/GPIO.

Useful when a photo taken on the Pi yields no numbers there, but does
locally with "some" YOLO processing: this lets you check whether the cause
is the pipeline (resizing, conf/iou thresholds, model version) or actually
the image/model itself.

Usage:
    cd backend
    .venv/bin/python debugPredict.py /path/to/photo.jpg

Requirements: ultralytics/torch/opencv-python are installed (see
requirements-pi.txt) -- they are missing from the normal dev .venv, since
they are only needed on the Pi for the camera pipeline:
    .venv/bin/pip install -r requirements-pi.txt

and model_digits/model_needles are set in watermeter/.env (see
watermeter/.env.example) -- locally they are empty by default.
"""

import argparse
import os
import sys

import cv2
from dotenv import dotenv_values
from PIL import Image

# Right next to readTotalConsumption.py, so "from results import ..."
# works inside the imported functions.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from results import ResultsExtended, plot_bboxes
from readTotalConsumption import (
    _PredictWorkerHandle,
    _makeInferenceImage,
    predictNeedlesAndCounterArea,
    predictDigits,
    getIntegerFromPredictions,
    identifyRedDigitsCorrectionFactor,
    identifyNeedlesCorrectionFactor,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("imagePath", help="path to the photo to test")
    parser.add_argument(
        "--env",
        default=os.path.join(os.path.dirname(__file__), "..", "watermeter", ".env"),
        help="path to the .env with model_digits/model_needles (default: watermeter/.env)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="show the result image with bounding boxes in a window",
    )
    args = parser.parse_args()

    config = dotenv_values(args.env)
    if not config.get("model_digits") or not config.get("model_needles"):
        sys.exit(
            f"model_digits/model_needles are not set in {args.env}. "
            "See watermeter/.env.example."
        )

    imagePath = os.path.abspath(args.imagePath)
    if not os.path.isfile(imagePath):
        sys.exit(f"Image not found: {imagePath}")

    # Exactly the same downscaling as in gettotalconsumption().
    inferenceImagePath = _makeInferenceImage(imagePath, config)
    print(f"Original: {imagePath}")
    print(f"Inference image (after resize): {inferenceImagePath}")

    worker = _PredictWorkerHandle()
    try:
        resultNeedles = predictNeedlesAndCounterArea(
            inferenceImagePath, config["model_needles"], worker
        )
        resultNeedlesExt = ResultsExtended(resultNeedles)
        resultNeedlesExt.sort_boxes(mode="r2l")
        flowNeedles, nNeedlesDetected = getIntegerFromPredictions(resultNeedlesExt)
        print(f"\n[Needles] detected classes: {resultNeedlesExt.boxes.cls.tolist()}")
        print(f"[Needles] confidences: {resultNeedlesExt.boxes.conf.tolist()}")
        print(f"[Needles] flowNeedles = {flowNeedles} (from {nNeedlesDetected} boxes)")

        result = predictDigits(inferenceImagePath, config["model_digits"], worker)
        resultExt = ResultsExtended(result)
        resultExt.sort_boxes()
        print(f"\n[Digits] detected classes: {resultExt.boxes.cls.tolist()}")
        print(f"[Digits] confidences: {resultExt.boxes.conf.tolist()}")

        flow, nDigitsDetected = getIntegerFromPredictions(resultExt)
        print(f"[Digits] flow = {flow} (from {nDigitsDetected} boxes)")

        if flowNeedles is None or flow is None:
            print(
                "\n=> No detection (at least one of the two stages returned "
                "no digit boxes) -- this matches the 'no numbers detected' "
                "case on the Pi."
            )
        else:
            digitsCorrectionFactor, nDigitsRed = identifyRedDigitsCorrectionFactor(resultExt)
            needlesCorrectionFactor = identifyNeedlesCorrectionFactor(
                resultNeedlesExt, nDigitsRed
            )
            totalconsumption = round(
                flow * digitsCorrectionFactor + flowNeedles * needlesCorrectionFactor, 5
            )
            print(f"\n=> totalconsumption = {totalconsumption}")

        img = plot_bboxes(resultNeedlesExt, resultExt)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        outPath = imagePath + "_bbox_debug.jpg"
        Image.fromarray(img).save(outPath)
        print(f"\nResult image with bounding boxes saved to: {outPath}")

        if args.show:
            cv2.imshow("debugPredict", img)
            cv2.waitKey(0)
            cv2.destroyAllWindows()
    finally:
        worker.shutdown()
        if inferenceImagePath != imagePath:
            try:
                os.remove(inferenceImagePath)
            except OSError:
                pass


if __name__ == "__main__":
    main()
