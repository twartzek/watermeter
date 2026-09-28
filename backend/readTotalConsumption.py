from ultralytics import YOLO
import cv2
import numpy as np
from db import store_reading
from results import ResultsExtended, plot_bboxes
import platform
import datetime
import gc
import multiprocessing
import os
import tempfile
import time
from dotenv import dotenv_values
from mqtt import mqtt_publish
from gpio import Led
from mylog import Logger
from scipy.ndimage import rotate
from PIL import Image
from outlierDetection import (
    missingDigitDetector,
    maxFlowDetector,
    negativeDeltaDetector,
    meterReplacementDetector,
    missingNeedleOrDigitDetector,
)

config = dotenv_values("watermeter/.env")


logger = Logger("readTotalConsumption")

try:
    from picamera2 import Picamera2
except ImportError:
    pass

led = Led(2)

# Auf dem Pi Zero 2 W (416 MB RAM) hat sich gezeigt, dass eine haengende
# YOLO-Inferenz (z.B. durch extremes Swap-Thrashing) nicht durch eine
# Exception auffaellt, sondern den Prozess unbegrenzt blockiert. Der externe
# `timeout` in startMeasurement.sh (siehe dort) greift erst danach und der
# watchdog-Dateicheck erst nach 15 Minuten, was sich wie ein kompletter
# Hänger des Pi anfuehlt. Dieses Timeout bricht einen einzelnen
# YOLO-predict-Aufruf deutlich frueher kontrolliert ab.
#
# Urspruenglich per signal.alarm(SIGALRM) umgesetzt -- das hat sich in der
# Praxis als wirkungslos erwiesen: CPython liefert SIGALRM nur zwischen
# Bytecode-Instruktionen aus, ein haengender nativer PyTorch/OpenCV-Aufruf
# (insbesondere unter Swap-Thrashing, wo der Aufruf im Kernel auf I/O
# wartet) kommt nie zu einem solchen Check-Punkt zurueck und der Alarm bleibt
# unzugestellt. Logs vom Pi zeigten 0 YoloTimeoutError bei >550 nie beendeten
# Laeufen -- der Watchdog-Reboot war in der Praxis der einzige Ausweg.
# Ein multiprocessing.Process laesst sich dagegen vom Betriebssystem per
# SIGKILL hart beenden, unabhaengig davon, ob der Kindprozess in nativem Code
# haengt.
#
# 300s erwies sich auf dem Pi Zero 2 W als zu knapp: alleine der einmalige
# ultralytics-Import + Modell-Laden im Worker (siehe _PredictWorkerHandle)
# braucht dort gemessen ~85s+~30s=~115s, WOVON DIESES TIMEOUT AB DEM START
# DES JEWEILIGEN predict()-AUFRUFS MITGERECHNET WIRD (der Import passiert
# beim allerersten Aufruf innerhalb des ersten run_predict()) -- unter
# Systemlast/Swap-Druck reichte das uebrige Budget fuer die eigentliche
# Inferenz nicht mehr zuverlaessig aus (live auf dem Pi beobachtet: ein
# einzelner predict()-Aufruf allein > 300s, ohne jede Nebenlast). 550s lassen
# beiden predict()-Aufrufen ausreichend Luft. Der aeussere `timeout` in
# startMeasurement.sh MUSS bei einer Aenderung hier konsistent mit angepasst
# werden (deutlich groesser als 2x dieser Wert, da beide predict()-Aufrufe
# nacheinander in dieses aeussere Timeout passen muessen), sonst wuerde er
# vor diesem inneren, kontrollierten Timeout zuschlagen.
YOLO_PREDICT_TIMEOUT_SECONDS = 550


class YoloTimeoutError(Exception):
    pass


# Import von ultralytics + das Laden eines Modells dauern auf dem Pi Zero 2 W
# gemessen ~85s bzw. ~30s (siehe git history/PR-Diskussion) -- der teure
# Import darf daher nicht pro predict()-Aufruf wiederholt werden, sonst ist
# der YOLO_PREDICT_TIMEOUT_SECONDS-Timeout schon vor der eigentlichen
# Inferenz aufgebraucht (genau das ist der Bug, den dieser langlebige Worker
# behebt). Das Modell selbst wird bewusst NICHT ueber mehrere modelpaths
# hinweg gecached: Needle- und Digit-Modell gleichzeitig im Speicher zu
# halten, sprengt das Pi Zero 2 W (416 MB RAM knapp) -- das war schon vor
# der multiprocessing-Umstellung die Regel (del model; gc.collect() nach
# jedem predict()). Der Worker haelt daher maximal ein geladenes Modell
# gleichzeitig: wechselt der naechste Request auf einen anderen modelpath,
# wird das vorherige Modell zuerst freigegeben.
_worker_model = None
_worker_modelpath = None


def _predict_worker(request_queue, result_queue):
    """
    Laeuft im langlebigen Kindprozess (siehe _PredictWorkerHandle).
    Nimmt (modelpath, imagePath, predict_kwargs)-Requests von der
    request_queue entgegen, bis None als Sentinel kommt. Haelt zu jedem
    Zeitpunkt hoechstens ein Modell geladen (siehe Kommentar oben).
    """
    global _worker_model, _worker_modelpath
    while True:
        request = request_queue.get()
        if request is None:
            return
        modelpath, imagePath, predict_kwargs = request
        try:
            if _worker_modelpath != modelpath:
                del _worker_model
                gc.collect()
                _worker_model = YOLO(modelpath, task="detect")
                _worker_modelpath = modelpath
            results = _worker_model.predict(imagePath, **predict_kwargs)
            result_queue.put(("ok", results[0]))
        except Exception as e:
            result_queue.put(("error", e))


class _PredictWorkerHandle:
    """
    Haelt den langlebigen Worker-Prozess plus seine Queues fuer die Dauer
    eines readTotalConsumption-Laufs. Ueber run_predict() koennen beliebig
    viele predict()-Auftraege nacheinander an denselben Prozess geschickt
    werden, ohne dass ultralytics/das Modell erneut geladen werden muss.
    Nur bei einem tatsaechlichen Haenger/Crash wird der Prozess beendet --
    ein Folgeauftrag startet dann automatisch einen frischen Worker, statt
    den ganzen readTotalConsumption-Lauf abzubrechen.
    """

    def __init__(self):
        self._ctx = multiprocessing.get_context("spawn")
        self._process = None
        self._request_queue = None
        self._result_queue = None

    def _ensure_started(self):
        if self._process is not None and self._process.is_alive():
            return
        self._request_queue = self._ctx.Queue()
        self._result_queue = self._ctx.Queue()
        self._process = self._ctx.Process(
            target=_predict_worker,
            args=(self._request_queue, self._result_queue),
        )
        self._process.start()

    def _kill(self):
        if self._process is None:
            return
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(10)
            if self._process.is_alive():
                # terminate() (SIGTERM) blieb wirkungslos -- typisch, wenn
                # der Prozess in einem unterbrechbaren nativen Aufruf haengt.
                # kill() (SIGKILL) kann vom Prozess nicht mehr
                # abgefangen/ignoriert werden.
                self._process.kill()
                self._process.join()
        self._process = None

    def run_predict(self, modelpath, imagePath, **predict_kwargs):
        """
        Fuehrt YOLO(modelpath).predict(imagePath, **predict_kwargs) im
        langlebigen Worker-Prozess aus und liefert results[0] zurueck.
        Startet den Worker bei Bedarf (erster Aufruf, oder nach einem
        vorherigen Timeout/Crash). Ueberschreitet dieser Aufruf
        YOLO_PREDICT_TIMEOUT_SECONDS, wird der Worker hart beendet und
        YoloTimeoutError geworfen, statt den Hauptprozess (und damit den
        Watchdog-Dateicheck in db.log) unbegrenzt zu blockieren.
        """
        self._ensure_started()
        self._request_queue.put((modelpath, imagePath, predict_kwargs))

        try:
            status, payload = self._result_queue.get(timeout=YOLO_PREDICT_TIMEOUT_SECONDS)
        except Exception:
            message = f"YOLO predict() did not finish within {YOLO_PREDICT_TIMEOUT_SECONDS}s"
            logger.logger.error(message)
            self._kill()
            raise YoloTimeoutError(message)

        if status == "error":
            # Der Worker meldet eine echte Exception aus predict() zurueck --
            # der Prozess selbst lebt noch (naechster Request kann denselben
            # Worker wiederverwenden, ultralytics-Import bleibt erhalten).
            raise payload
        return payload

    def shutdown(self):
        if self._process is None:
            return
        if self._process.is_alive():
            try:
                self._request_queue.put(None)
                self._process.join(5)
            except Exception:
                pass
        self._kill()


def mad(data):
    # Calculate the median of the data
    median = np.median(data)

    # Calculate the absolute deviations from the median
    deviations = [abs(x - median) for x in data]

    # Calculate the median of the absolute deviations
    mad = np.median(deviations)
    return mad


def sum_red_pixels(result):
    """
    Calculate the total sum of all red pixels in each bounding box.

    Args:
        image (numpy array): Input image.
        bounding_boxes (list): List of bounding boxes in the format [x, y, w, h].

    Returns:
        list: List of sums of red pixels in each bounding box.
    """
    # Split the image into BGR channels
    image = result.orig_img
    # Convert BGR to HSV
    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    # define range of red color in HSV
    lower_red1 = np.array([0, 120, 70])
    upper_red1 = np.array([10, 255, 255])
    lower_red2 = np.array([170, 120, 70])
    upper_red2 = np.array([180, 255, 255])

    mask1 = cv2.inRange(hsv_image, lower_red1, upper_red1)
    mask2 = cv2.inRange(hsv_image, lower_red2, upper_red2)

    red_mask = mask1 + mask2
    # Threshold the HSV image to get only red pixels
    imgWithMask = cv2.bitwise_and(image, image, mask=red_mask)

    # show for debugging
    # cv2.imshow("ROI", image)
    # cv2.waitKey(0)
    # cv2.destroyAllWindows()

    # Initialize a list to store the sum of red pixels in each bounding box
    red_sums = []

    # Iterate over each bounding box
    for box in result.boxes.xyxy.numpy().astype(np.int32):
        # Extract the region of interest (ROI) from the red channel
        x1, y1, x2, y2 = box

        roi = imgWithMask[y1:y2, x1:x2]

        # Calculate the sum of red pixels in the ROI
        red_sum = np.sum(roi)

        # Append the sum to the list
        red_sums.append(red_sum)

    return red_sums


def getRawDigitsFromPredictions(result) -> list[int]:
    """
    Rohe Ziffernfolge (MSB zuerst, wie von sort_boxes() sortiert) direkt aus
    den YOLO-Klassen.
    """
    return [int(x) for x in result.boxes.cls.numpy() if x < 10]


def getIntegerFromPredictions(result):
    """
    Eigenstaendige Variante fuer Faelle, die NUR eine Domaene (Nadeln ODER
    Digits) betrachten (aktuell: debugPredict.py, das beide unabhaengig
    voneinander inspiziert).

    Returns:
    tuple[float|None, int]: (Zahl aus den rohen YOLO-Klassen, oder None
        wenn keine Box erkannt wurde; Anzahl der dafuer verwendeten Boxen).
    """
    digitsInteger = getRawDigitsFromPredictions(result)
    nBoxes = len(digitsInteger)
    if nBoxes == 0:
        return None, 0
    value = float("".join(map(str, digitsInteger)))
    return value, nBoxes


def predictDigits(imagePath, modelpath, worker: "_PredictWorkerHandle"):
    # Modelle werden im langlebigen Worker-Kindprozess geladen (siehe
    # _PredictWorkerHandle/_predict_worker) -- weder hier noch im Worker ist
    # je mehr als ein Modell gleichzeitig im Speicher (siehe Kommentar bei
    # _predict_worker), das Kindprozess-Modell wird mit dem Prozess selbst
    # wieder freigegeben.
    return worker.run_predict(
        modelpath,
        imagePath,
        save=False,
        imgsz=640,
        agnostic_nms=True,
        conf=0.6,
    )


def predictNeedlesAndCounterArea(imagePath, modelpath, worker: "_PredictWorkerHandle"):
    return worker.run_predict(
        modelpath,
        imagePath,
        save=False,
        imgsz=640,
        agnostic_nms=True,
        conf=0.5,
        iou=0.3,
    )


def identifyRedDigitsCorrectionFactor(result):
    sumReds = np.array(sum_red_pixels(result))
    madThreshold = mad(sumReds)
    whichAreReds = sumReds > 10 * madThreshold
    nDigitsRed = sum(whichAreReds == True)
    correctionFactor = float(1 / pow(10, nDigitsRed))
    return correctionFactor, nDigitsRed


def identifyNeedlesCorrectionFactor(result, nDigitsRed):
    nNeedles = 0
    for box in result.boxes:
        if int(box.cls.numpy()[0]) < 10:
            nNeedles += 1
    # nNeedles = len(result.boxes.cls)

    correctionFactor = float(1 / pow(10, nDigitsRed + nNeedles))
    return correctionFactor


def maskImage(result, classId: int, show=False):
    img = result.orig_img
    # Create a mask image with the same size as the original image
    mask = np.zeros(img.shape, dtype=np.uint8)

    for box in result.boxes:
        if (int(box.cls.numpy()[0])) == classId:
            x1, y1, x2, y2 = box.xyxy.numpy().astype(np.int32)[0]

            # Draw a rectangle on the mask image to represent the bounding box
            cv2.rectangle(mask, (x1, y1), (x2, y2), (255, 255, 255), -1)

    # Apply the mask to the original image
    masked_img = cv2.bitwise_and(img, mask)

    # Display the masked image
    if show:
        cv2.imshow("Masked Image", masked_img)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    return masked_img


def _makeInferenceImage(imagePath, config, max_side=1280):
    """
    Erstellt neben dem in voller Aufloesung gespeicherten Originalfoto ein
    verkleinertes Zwischenbild fuer die YOLO-Inferenz. Ohne das wuerde
    ultralytics das Originalbild (2592x1944) fuer jeden der zwei
    predict()-Aufrufe komplett neu von Disk laden und in voller Aufloesung
    im Speicher halten, bevor intern auf imgsz=640 skaliert wird -- auf dem
    Pi Zero 2 W (416 MB RAM) unnoetig speicherintensiv. max_side=1280 liegt
    deutlich ueber imgsz=640 und laesst so mehr Detail (z.B. den duennen
    Mittelsteg, der eine 8 von einer 0 unterscheidet) bis kurz vor YOLOs
    eigenes Resizing erhalten; vorher fuehrte max_side=800 in Kombination
    mit BILINEAR gelegentlich dazu, dass 8en wie 0en aussahen.
    Das Originalfoto in data/images bleibt unveraendert, da es unveraendert
    ueber die API ausgeliefert wird (siehe restapi.py).
    """
    img = Image.open(imagePath)
    scale = max_side / max(img.size)
    if scale >= 1:
        return imagePath
    newSize = (round(img.width * scale), round(img.height * scale))
    # LANCZOS statt BILINEAR: schaerferes Downscaling, das feine
    # Ziffern-Strukturen (duenne Stege/Kanten) besser erhaelt als die
    # staerker glaettende bilineare Interpolation -- siehe Docstring oben.
    resized = img.resize(newSize, Image.LANCZOS)
    # Bewusst NICHT in config["images"]: get_newest_image() (db.py) scannt
    # genau dieses Verzeichnis fuer den Entwicklermodus-Fallback und wuerde
    # ein liegen gebliebenes Zwischenbild (z.B. nach einem harten Absturz,
    # bevor das finally in gettotalconsumption() aufraeumen konnte) sonst
    # faelschlich als neuestes Foto anzeigen.
    inferencePath = os.path.join(
        tempfile.gettempdir(), "watermeter_inference_" + os.path.basename(imagePath)
    )
    resized.save(inferencePath)
    return inferencePath


def gettotalconsumption(imagePath, config, debug=False):
    logger.logger.info("Started gettotalconsumption")
    inferenceImagePath = _makeInferenceImage(imagePath, config)
    # Ein Worker-Prozess fuer beide predict()-Aufrufe dieses Laufs: der teure
    # ultralytics-Import (siehe Kommentar bei _predict_worker) passiert so
    # nur einmal statt zweimal. Wird nach diesem Lauf immer beendet -- kein
    # Prozess bleibt zwischen Cron-Aufrufen von readTotalConsumption.py am
    # Leben.
    worker = _PredictWorkerHandle()
    try:
        return _gettotalconsumption(imagePath, inferenceImagePath, config, worker, debug=debug)
    finally:
        worker.shutdown()
        if inferenceImagePath != imagePath:
            try:
                os.remove(inferenceImagePath)
            except OSError:
                pass


def _gettotalconsumption(originalImagePath, imagePath, config, worker, debug=False):
    # Needle Detection
    resultNeedles = predictNeedlesAndCounterArea(imagePath, config["model_needles"], worker)

    resultNeedlesExt = ResultsExtended(resultNeedles)
    resultNeedlesExt.sort_boxes(mode="r2l")
    needleDigits = getRawDigitsFromPredictions(resultNeedlesExt)
    nNeedlesDetected = len(needleDigits)

    if nNeedlesDetected == 0:
        return None, None, None, None, None, None

    # masked_counter = maskImage(resultNeedlesExt, 10, show=False)

    # Digits Detection
    result = predictDigits(imagePath, config["model_digits"], worker)
    resultExt = ResultsExtended(result)
    resultExt.sort_boxes()

    # Filter boxes which are in counterBox area
    # counterBox = resultNeedlesExt.get_bboxes_of_class(10)[0]
    # resultExt.filterbboxes(counterBox.xyxy.tolist()[0])

    digitsCorrectionFactor, nDigitsRed = identifyRedDigitsCorrectionFactor(resultExt)
    displayDigits = getRawDigitsFromPredictions(resultExt)
    nDigitsDetected = len(displayDigits)

    if nDigitsDetected == 0:
        return None, None, None, None, None, None

    flow = float("".join(map(str, displayDigits)))
    flowNeedles = float("".join(map(str, needleDigits)))

    # Calculate the total flow
    needlesCorrectionFactor = identifyNeedlesCorrectionFactor(
        resultNeedlesExt, nDigitsRed
    )
    # beforeMissingNeedleCheck: der Rohwert, wie er OHNE Ruecksicht auf
    # missingNeedleOrDigitDetector() aus den erkannten Boxen berechnet wuerde.
    # Wird bewusst immer berechnet (auch wenn der Detector gleich verwirft),
    # damit store_reading() ihn mit ablegen kann -- siehe Kommentar unten.
    beforeMissingNeedleCheck = round(
        flow * digitsCorrectionFactor + flowNeedles * needlesCorrectionFactor, 5
    )

    img = plot_bboxes(resultNeedlesExt, resultExt)
    imgOutPath = os.path.join(
        config["images"], os.path.basename(originalImagePath) + "_bbox.jpg"
    )
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    pilimg = Image.fromarray(img)
    pilimg.save(imgOutPath)
    # cv2.imwrite(imgOutPath, img) # this seemed to lead to non predicatable crashes / memory leaks
    logger.logger.info("Saved bbox image to " + imgOutPath)

    if debug and not is_raspberry_pi():
        cv2.imshow("img", img)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    # Vergleicht die Anzahl der in DIESEM Bild erkannten Nadel-/Digit-Boxen
    # gegen die aus der History gelernte erwartete Anzahl (siehe
    # outlierDetection.missingNeedleOrDigitDetector) -- eine gegenueber der
    # History fehlende Box (z.B. eine schlecht erkannte Nadel) wuerde sonst
    # unbemerkt alle uebrigen Ziffern um eine Zehnerpotenz verschieben.
    # Laeuft bewusst NACH der totalconsumption-Berechnung UND nach dem
    # Speichern des Bbox-Bilds (anders als davor): so werden
    # beforeMissingNeedleCheck und das Bbox-Bild (mit den erkannten Boxen
    # inkl. der fehlenden Stelle) auch bei Verdacht erzeugt und ueber
    # store_reading() (siehe __main__-Block) mit abgelegt -- der
    # Entwicklermodus im Frontend (CardLastPhoto.jsx) kann so den Rohwert,
    # die Bounding-Boxen UND den Verwurfsgrund zu einer verworfenen Messung
    # anzeigen, statt dass sie spurlos verschwindet.
    discardReason = missingNeedleOrDigitDetector(nNeedlesDetected, nDigitsDetected)
    if discardReason is not None:
        return (
            None, None, nNeedlesDetected, nDigitsDetected, beforeMissingNeedleCheck,
            discardReason,
        )

    results = [resultNeedlesExt, resultExt]

    return (
        beforeMissingNeedleCheck, results, nNeedlesDetected, nDigitsDetected,
        beforeMissingNeedleCheck, None,
    )


def is_raspberry_pi():
    return (
        platform.machine() == "armv7l"
        or platform.machine() == "armv6l"
        or platform.machine() == "aarch64"
    )


def getImage():
    picam2 = Picamera2()
    # mode = picam2.sensor_modes[0]
    # camera_config = picam2.create_still_configuration(sensor={"output_size": mode["size"], "bit_depth":mode["bit_depth"]})
    camera_config = picam2.create_still_configuration()
    picam2.configure(camera_config)
    led.on()
    picam2.start()
    time.sleep(1)
    now = datetime.datetime.now()
    imageName = now.strftime("%Y-%m-%d_%H-%M-%S") + ".jpg"

    # Das Bild wird als numpy-Array erfasst
    image_array = picam2.capture_array()

    # Das Bild wird um 90 Grad gegen den Uhrzeigersinn gedreht
    # (vorher 120 Grad gegen den Uhrzeigersinn, jetzt zusaetzlich 30 Grad im
    # Uhrzeigersinn korrigiert, da die Kamera nicht exakt ausgerichtet ist)
    rotated_array = rotate(image_array, 90, reshape=False)

    filepath = os.path.join(config["images"], imageName)
    pil_img = Image.fromarray(rotated_array)
    pil_img.save(filepath)
    logger.logger.info("Saved image to " + filepath)
    led.off()
    picam2.close()
    return filepath


if __name__ == "__main__":
    logger.logger.info("Started readTotalConsumption main")

    if is_raspberry_pi():
        try:
            imagePath = getImage()
        except Exception as e:
            logger.logger.error("Image capture failed. Exiting readTotalConsumption")
            logger.logger.error(e)
            exit()
    else:
        # For testing purpose on your desktop
        # Change here if needed
        # imagePath = "image.jpg"
        logger.logger.warning("No camera detected. Exiting readTotalConsumption")
        exit()

    try:
        (
            totalconsumption, results, nNeedlesDetected, nDigitsDetected,
            beforeMissingNeedleCheck, discardReason,
        ) = gettotalconsumption(imagePath, config, debug=False)
    except Exception as e:
        logger.logger.error(e)
        exit()

    logger.logger.info(totalconsumption)

    if totalconsumption is None:
        # Erkennung fehlgeschlagen (keine Boxen erkannt, ODER von
        # missingNeedleOrDigitDetector wegen einer gegenueber der History
        # fehlenden Nadel-/Digit-Box verworfen -- siehe _gettotalconsumption)
        # -- trotzdem einen DB-Eintrag anlegen (mit totalconsumption=None),
        # damit das aufgenommene Bild ueber die API auffindbar ist (siehe
        # Entwicklermodus in den Einstellungen). Verbrauchsauswertungen
        # filtern NULL-Werte bereits per totalconsumption.is_null(False)
        # heraus und sind daher nicht betroffen; ebenso fliessen solche
        # Readings dadurch nicht in die von missingNeedleOrDigitDetector
        # gelernte Erwartungshistorie ein.
        #
        # beforeMissingNeedleCheck traegt in diesem Zweig entweder None
        # (gar keine Boxen erkannt) oder den Rohwert, den
        # missingNeedleOrDigitDetector verworfen hat -- afterMissingNeedleCheck
        # ist in beiden Faellen None, da die Messung nicht in die Pipeline
        # eingeflossen ist. So bleibt in der DB nachtraeglich sichtbar,
        # welchen (potenziell falschen) Wert eine verworfene Messung ergeben
        # haette, statt dass er spurlos verloren geht.
        store_reading(
            None, None, imagePath,
            nNeedlesDetected=nNeedlesDetected, nDigitsDetected=nDigitsDetected,
            beforeMissingNeedleCheck=beforeMissingNeedleCheck, afterMissingNeedleCheck=None,
            discardReason=discardReason,
        )
        logger.logger.info(
            "No total consumption detected. Exiting readTotalConsumption"
        )
        exit()
    else:
        # Beobachtet den rohen Messwert auf das Muster eines Zaehlertauschs
        # (siehe outlierDetection.py) und benachrichtigt den Nutzer bei
        # Verdacht. Greift nicht in outlierfiltered ein -- laeuft daher vor
        # den anderen Filtern, die den rohen Wert sonst unveraendert
        # weiterreichen wuerden.
        meterReplacementDetector(totalconsumption)
        # Zwischenwert nach jedem einzelnen Filterschritt wird mit
        # gespeichert (siehe db.Reading.afterMissingDigit/afterMaxFlow/
        # afterNegativeDelta) -- sonst laesst sich aus totalconsumption+
        # filtered allein nicht rekonstruieren, welcher der drei Filter
        # einen gegebenen Wert veraendert hat.
        afterMissingDigit = missingDigitDetector(totalconsumption)
        afterMaxFlow = maxFlowDetector(afterMissingDigit)
        afterNegativeDelta = negativeDeltaDetector(afterMaxFlow)
        outlierfiltered = afterNegativeDelta
        store_reading(
            totalconsumption, outlierfiltered, imagePath,
            nNeedlesDetected=nNeedlesDetected, nDigitsDetected=nDigitsDetected,
            afterMissingDigit=afterMissingDigit, afterMaxFlow=afterMaxFlow,
            afterNegativeDelta=afterNegativeDelta,
            beforeMissingNeedleCheck=beforeMissingNeedleCheck,
            afterMissingNeedleCheck=totalconsumption,
        )

    mqtt_publish("watermeter/consumption/total/raw", totalconsumption)
    mqtt_publish("watermeter/consumption/total/outlierfiltered", outlierfiltered)
    logger.logger.info("Finished readTotalConsumption main")
