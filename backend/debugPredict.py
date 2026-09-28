"""
Debug-Skript, um ein einzelnes Foto lokal (Desktop) durch EXAKT dieselbe
YOLO-Erkennungspipeline zu schicken, die auf dem Pi in
readTotalConsumption.py laeuft (gleiches Preprocessing, gleiche Modelle,
gleiche predict()-Parameter) -- nur ohne Kamera/DB/MQTT/GPIO.

Nuetzlich, wenn ein auf dem Pi aufgenommenes Foto dort keine Zahlen liefert,
lokal mit "irgendeiner" YOLO-Verarbeitung aber schon: hiermit laesst sich
pruefen, ob es an der Pipeline (Resizing, conf/iou-Schwellen, Modellversion)
liegt oder tatsaechlich am Bild/Modell selbst.

Nutzung:
    cd backend
    .venv/bin/python debugPredict.py /pfad/zum/foto.jpg

Voraussetzung: ultralytics/torch/opencv-python sind installiert (siehe
requirements-pi.txt) -- diese fehlen im normalen Dev-.venv, da sie nur auf
dem Pi fuer die Kamera-Pipeline gebraucht werden:
    .venv/bin/pip install -r requirements-pi.txt

sowie model_digits/model_needles in watermeter/.env gesetzt (siehe
watermeter/.env.example) -- lokal sind diese standardmaessig leer.
"""

import argparse
import os
import sys

import cv2
from dotenv import dotenv_values
from PIL import Image

# Direkt neben readTotalConsumption.py, damit "from results import ..."
# innerhalb der importierten Funktionen funktioniert.
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
    parser.add_argument("imagePath", help="Pfad zum zu testenden Foto")
    parser.add_argument(
        "--env",
        default=os.path.join(os.path.dirname(__file__), "..", "watermeter", ".env"),
        help="Pfad zur .env mit model_digits/model_needles (default: watermeter/.env)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Ergebnisbild mit bounding boxes in einem Fenster anzeigen",
    )
    args = parser.parse_args()

    config = dotenv_values(args.env)
    if not config.get("model_digits") or not config.get("model_needles"):
        sys.exit(
            f"model_digits/model_needles sind in {args.env} nicht gesetzt. "
            "Siehe watermeter/.env.example."
        )

    imagePath = os.path.abspath(args.imagePath)
    if not os.path.isfile(imagePath):
        sys.exit(f"Bild nicht gefunden: {imagePath}")

    # Exakt dasselbe Downscaling wie in gettotalconsumption().
    inferenceImagePath = _makeInferenceImage(imagePath, config)
    print(f"Original: {imagePath}")
    print(f"Inferenzbild (nach Resize): {inferenceImagePath}")

    worker = _PredictWorkerHandle()
    try:
        resultNeedles = predictNeedlesAndCounterArea(
            inferenceImagePath, config["model_needles"], worker
        )
        resultNeedlesExt = ResultsExtended(resultNeedles)
        resultNeedlesExt.sort_boxes(mode="r2l")
        flowNeedles, nNeedlesDetected = getIntegerFromPredictions(resultNeedlesExt)
        print(f"\n[Needles] erkannte Klassen: {resultNeedlesExt.boxes.cls.tolist()}")
        print(f"[Needles] confidences: {resultNeedlesExt.boxes.conf.tolist()}")
        print(f"[Needles] flowNeedles = {flowNeedles} (aus {nNeedlesDetected} Boxen)")

        result = predictDigits(inferenceImagePath, config["model_digits"], worker)
        resultExt = ResultsExtended(result)
        resultExt.sort_boxes()
        print(f"\n[Digits] erkannte Klassen: {resultExt.boxes.cls.tolist()}")
        print(f"[Digits] confidences: {resultExt.boxes.conf.tolist()}")

        flow, nDigitsDetected = getIntegerFromPredictions(resultExt)
        print(f"[Digits] flow = {flow} (aus {nDigitsDetected} Boxen)")

        if flowNeedles is None or flow is None:
            print(
                "\n=> Keine Erkennung (mind. eine der beiden Stufen lieferte "
                "keine Ziffern-Boxen) -- das entspricht dem 'keine Zahlen "
                "erkannt'-Fall auf dem Pi."
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
        print(f"\nErgebnisbild mit bounding boxes gespeichert unter: {outPath}")

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
