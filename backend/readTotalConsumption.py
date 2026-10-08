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

# Fixed exposure instead of auto exposure: the LED is the only light source
# in the meter pit, and auto exposure blew out the blue channel on large
# parts of the image (see exposureTest.py). With the black cardboard lining
# inside the housing (against reflections on the digit window) only ~1/4 of
# the light reaches the meter, so 160 ms at gain 1.0 is needed for a dial
# brightness of ~155 (blue channel) without clipping in the dial/digit
# window; 40 ms (the value before the lining) was visibly too dark.
# Re-run exposureTest.py after changing LED/housing/lining.
CAMERA_CONTROLS = {"AeEnable": False, "ExposureTime": 160000, "AnalogueGain": 1.0}

# On the Pi Zero 2 W (416 MB RAM) it turned out that a hanging YOLO
# inference (e.g. due to extreme swap thrashing) doesn't surface as an
# exception but blocks the process indefinitely. The external `timeout` in
# startMeasurement.sh (see there) only kicks in later and the watchdog file
# check only after 15 minutes, which feels like the whole Pi has frozen.
# This timeout aborts a single YOLO predict call much earlier and in a
# controlled way.
#
# Originally implemented with signal.alarm(SIGALRM) -- which proved
# ineffective in practice: CPython only delivers SIGALRM between bytecode
# instructions, and a hanging native PyTorch/OpenCV call (especially under
# swap thrashing, where the call waits on I/O in the kernel) never returns
# to such a checkpoint, so the alarm is never delivered. Logs from the Pi
# showed 0 YoloTimeoutError for >550 runs that never finished -- the
# watchdog reboot was the only way out in practice. A
# multiprocessing.Process, on the other hand, can be killed hard by the
# operating system via SIGKILL, regardless of whether the child process is
# stuck in native code.
#
# 300s turned out to be too tight on the Pi Zero 2 W: the one-time
# ultralytics import + model loading in the worker (see
# _PredictWorkerHandle) alone takes a measured ~85s+~30s=~115s there, WHICH
# COUNTS AGAINST THIS TIMEOUT FROM THE START OF THE RESPECTIVE predict()
# CALL (the import happens on the very first call within the first
# run_predict()) -- under system load/swap pressure the remaining budget was
# no longer reliably enough for the actual inference (observed live on the
# Pi: a single predict() call alone > 300s, without any other load). 550s
# leaves enough headroom for both predict() calls. The outer `timeout` in
# startMeasurement.sh MUST be adjusted consistently when changing this
# (clearly larger than 2x this value, since both predict() calls have to
# fit into that outer timeout one after the other), otherwise it would hit
# before this inner, controlled timeout.
YOLO_PREDICT_TIMEOUT_SECONDS = 550


class YoloTimeoutError(Exception):
    pass


# Importing ultralytics + loading a model take a measured ~85s and ~30s on
# the Pi Zero 2 W (see git history/PR discussion) -- so the expensive
# import must not be repeated for every predict() call, otherwise the
# YOLO_PREDICT_TIMEOUT_SECONDS timeout is used up before the actual
# inference (exactly the bug this long-lived worker fixes). The model
# itself is deliberately NOT cached across several modelpaths: keeping the
# needle and digit models in memory at the same time overwhelms the Pi Zero
# 2 W (416 MB RAM is tight) -- that was already the rule before the switch
# to multiprocessing (del model; gc.collect() after each predict()). The
# worker therefore holds at most one loaded model at a time: if the next
# request switches to a different modelpath, the previous model is freed
# first.
_worker_model = None
_worker_modelpath = None


def _predict_worker(request_queue, result_queue):
    """
    Runs in the long-lived child process (see _PredictWorkerHandle).
    Accepts (modelpath, imagePath, predict_kwargs) requests from
    request_queue until None arrives as a sentinel. Holds at most one model
    loaded at any time (see comment above).
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
    Holds the long-lived worker process plus its queues for the duration
    of one readTotalConsumption run. Any number of predict() jobs can be
    sent to the same process one after another via run_predict(), without
    reloading ultralytics/the model. The process is only terminated on an
    actual hang/crash -- a subsequent job then automatically starts a fresh
    worker instead of aborting the whole readTotalConsumption run.
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
                # terminate() (SIGTERM) had no effect -- typical when the
                # process is stuck in an uninterruptible native call.
                # kill() (SIGKILL) cannot be caught/ignored by the process.
                self._process.kill()
                self._process.join()
        self._process = None

    def run_predict(self, modelpath, imagePath, **predict_kwargs):
        """
        Runs YOLO(modelpath).predict(imagePath, **predict_kwargs) in the
        long-lived worker process and returns results[0]. Starts the worker
        if needed (first call, or after a previous timeout/crash). If this
        call exceeds YOLO_PREDICT_TIMEOUT_SECONDS, the worker is killed hard
        and YoloTimeoutError is raised, instead of blocking the main process
        (and with it the watchdog file check on db.log) indefinitely.
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
            # The worker reports a real exception from predict() -- the
            # process itself is still alive (the next request can reuse the
            # same worker, the ultralytics import is kept).
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
    Raw digit sequence (MSB first, as sorted by sort_boxes()) directly from
    the YOLO classes.
    """
    return [int(x) for x in result.boxes.cls.numpy() if x < 10]


def getIntegerFromPredictions(result):
    """
    Standalone variant for cases that look at ONLY one domain (needles OR
    digits) (currently: debugPredict.py, which inspects both
    independently).

    Returns:
    tuple[float|None, int]: (number from the raw YOLO classes, or None if
        no box was detected; number of boxes used for it).
    """
    digitsInteger = getRawDigitsFromPredictions(result)
    nBoxes = len(digitsInteger)
    if nBoxes == 0:
        return None, 0
    value = float("".join(map(str, digitsInteger)))
    return value, nBoxes


def predictDigits(imagePath, modelpath, worker: "_PredictWorkerHandle"):
    # Models are loaded in the long-lived worker child process (see
    # _PredictWorkerHandle/_predict_worker) -- neither here nor in the worker
    # is there ever more than one model in memory at a time (see comment at
    # _predict_worker); the child process's model is freed together with
    # the process itself.
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
    Creates a downscaled intermediate image for YOLO inference next to the
    full-resolution original photo. Without it, ultralytics would reload
    the original image (2592x1944) from disk for each of the two predict()
    calls and keep it in memory at full resolution before scaling it to
    imgsz=640 internally -- unnecessarily memory-hungry on the Pi Zero 2 W
    (416 MB RAM). max_side=1280 is well above imgsz=640 and so preserves
    more detail (e.g. the thin middle bar that distinguishes an 8 from a 0)
    until right before YOLO's own resizing; previously max_side=800
    combined with BILINEAR occasionally made 8s look like 0s.
    The original photo in data/images stays unchanged, since it is served
    as-is via the API (see restapi.py).
    """
    img = Image.open(imagePath)
    scale = max_side / max(img.size)
    if scale >= 1:
        return imagePath
    newSize = (round(img.width * scale), round(img.height * scale))
    # LANCZOS instead of BILINEAR: sharper downscaling that preserves fine
    # digit structures (thin bars/edges) better than the more smoothing
    # bilinear interpolation -- see docstring above.
    resized = img.resize(newSize, Image.LANCZOS)
    # Deliberately NOT in config["images"]: get_newest_image() (db.py) scans
    # exactly that directory for the developer mode fallback and would
    # otherwise wrongly show a leftover intermediate image (e.g. after a
    # hard crash, before the finally in gettotalconsumption() could clean
    # up) as the newest photo.
    inferencePath = os.path.join(
        tempfile.gettempdir(), "watermeter_inference_" + os.path.basename(imagePath)
    )
    resized.save(inferencePath)
    return inferencePath


def gettotalconsumption(imagePath, config, debug=False):
    logger.logger.info("Started gettotalconsumption")
    inferenceImagePath = _makeInferenceImage(imagePath, config)
    # One worker process for both predict() calls of this run: the
    # expensive ultralytics import (see comment at _predict_worker) thus
    # happens only once instead of twice. Always terminated after this run
    # -- no process stays alive between cron runs of
    # readTotalConsumption.py.
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
    # beforeMissingNeedleCheck: the raw value as it would be computed from
    # the detected boxes WITHOUT regard to missingNeedleOrDigitDetector().
    # Deliberately always computed (even if the detector discards it right
    # after), so store_reading() can store it too -- see comment below.
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

    # Compares the number of needle/digit boxes detected in THIS image
    # against the expected count learned from the history (see
    # outlierDetection.missingNeedleOrDigitDetector) -- a box missing
    # compared to the history (e.g. a poorly detected needle) would
    # otherwise silently shift all remaining digits by an order of
    # magnitude. Deliberately runs AFTER the totalconsumption calculation
    # AND after saving the bbox image (unlike before): that way
    # beforeMissingNeedleCheck and the bbox image (with the detected boxes,
    # including the missing position) are produced even on suspicion and
    # stored via store_reading() (see __main__ block) -- developer mode in
    # the frontend (CardLastPhoto.jsx) can then show the raw value, the
    # bounding boxes AND the discard reason for a discarded reading instead
    # of it vanishing without a trace.
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
    camera_config = picam2.create_still_configuration(controls=CAMERA_CONTROLS)
    picam2.configure(camera_config)
    led.on()
    picam2.start()
    time.sleep(1)
    now = datetime.datetime.now()
    imageName = now.strftime("%Y-%m-%d_%H-%M-%S") + ".jpg"

    # Capture the image as a numpy array
    image_array = picam2.capture_array()

    # Rotate the image 90 degrees counterclockwise
    # (previously 120 degrees counterclockwise, now corrected by an extra 30
    # degrees clockwise, since the camera is not exactly aligned)
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
        # Detection failed (no boxes detected, OR discarded by
        # missingNeedleOrDigitDetector because of a needle/digit box missing
        # compared to the history -- see _gettotalconsumption) -- still
        # create a DB entry (with totalconsumption=None), so the captured
        # image can be found via the API (see developer mode in the
        # settings). Consumption reports already filter out NULL values via
        # totalconsumption.is_null(False) and are therefore not affected;
        # likewise such readings don't flow into the expectation history
        # learned by missingNeedleOrDigitDetector.
        #
        # In this branch beforeMissingNeedleCheck holds either None (no
        # boxes detected at all) or the raw value that
        # missingNeedleOrDigitDetector discarded -- afterMissingNeedleCheck
        # is None in both cases, since the reading did not enter the
        # pipeline. That way the DB still shows afterwards which (potentially
        # wrong) value a discarded reading would have produced, instead of
        # it being lost without a trace.
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
        # Watches the raw reading for the pattern of a meter replacement
        # (see outlierDetection.py) and notifies the user on suspicion.
        # Does not affect outlierfiltered -- so it runs before the other
        # filters, which would otherwise pass the raw value on unchanged.
        meterReplacementDetector(totalconsumption)
        # The intermediate value after each individual filter step is
        # stored too (see db.Reading.afterMissingDigit/afterMaxFlow/
        # afterNegativeDelta) -- otherwise totalconsumption+filtered alone
        # can't tell which of the three filters changed a given value.
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

    # Immediate check for a sustained high flow (possible pipe burst), see
    # leakageDetector.detectSustainedHighFlow. Imported only here and
    # guarded: a failure in this check must never cost the reading itself,
    # which is already stored at this point.
    try:
        from leakageDetector import detectSustainedHighFlow
        detectSustainedHighFlow()
    except Exception as e:
        logger.logger.error(f"detectSustainedHighFlow failed: {e}")

    mqtt_publish("watermeter/consumption/total/raw", totalconsumption)
    mqtt_publish("watermeter/consumption/total/outlierfiltered", outlierfiltered)
    logger.logger.info("Finished readTotalConsumption main")
