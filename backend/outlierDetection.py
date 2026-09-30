import numpy as np
from db import (
    get_last_readings,
    addNotification,
    getKeyValueStoreValue,
    setKeyValueStoreValue,
    addMeterReplacement,
    getLastMeterReplacement,
)
from emailhandler import sendEmail
import pandas as pd
from datetime import datetime
from mylog import Logger

logger = Logger("outlierDetector")

MAXFLOW = 0.06 # 60l/min --> 0.06m³/min  --> 0.6 m³/10min

# Absolute upper bound for a single jump (dy), independent of the time
# elapsed since the last reading (dt). maxFlowDetector() actually checks a
# RATE (dy/dt <= MAXFLOW) -- that alone is not enough: if several readings
# in between were discarded (e.g. poor lighting), dt can grow large, so
# even a very large dy stays below the rate threshold. Observed in
# practice: a YOLO misreading (86.9 -> 87.9, probably a misread digit)
# jumped by 1.02 m³ at dt=75min (rate=0.0136 m³/min, far below MAXFLOW) and
# was therefore accepted, although it was by far the largest jump ever
# accepted in the whole history (next largest: 0.23 m³). This floor catches
# exactly that case without changing the rate check for normal, dense
# measurement intervals -- 0.4 m³ is well above the largest real single
# jump observed so far (shower/washing machine, ~0.1-0.23 m³), but far
# below an order-of-magnitude misreading.
MAXFLOW_ABSOLUTE_JUMP = 0.4  # m³, independent of dt

# --- Sustained high flow (burst pipe) vs. one-off outlier -----------------
#
# maxFlowDetector() ALWAYS compares against readings[-1].filtered -- the
# most recently STORED reading (which may itself already have been
# clamped). For a one-off detection error that is exactly right: the next
# real reading is close to the old, unchanged meter value again, so the
# reference point only "freezes" for one cycle.
#
# A REAL, sustained burst pipe with very high flow behaves differently:
# every new reading is compared against the old value (frozen by the
# previous clamp) and is therefore ALWAYS "too far away" relative to it --
# the stored value stays stuck at the old level for any number of cycles
# while the real meter keeps running. A burst pipe at 40 L/min over 3h
# would never become visible: filtered would stay constant although 7.2 m³
# actually leaked -- fatal, because exactly in this case (lots of water IS
# RUNNING OUT right now) detectLeakage() (leakageDetector.py) needs to see
# the increase in the raw data at all in order to raise an alarm.
#
# The distinction is the same as for meter replacement detection below:
# persistence. If several consecutive raw readings stay consistently close
# to each other AND above what still looks like a single error, it is no
# longer noise but a real, sustained trend -- from then on the current
# value is passed through instead of being clamped further, so the leak
# detectors can see it.
MAXFLOW_PERSISTENT_CONFIRM_COUNT = 3

# Tolerance within which consecutive raw readings still count as
# "consistently on the same sustained trend". Higher than
# METERREPLACEMENT_CONSISTENCY_TOLERANCE because here (unlike a meter
# replacement, which STAYS at a new level) the meter keeps rising during
# the observation itself -- the tolerance has to cover the consumption of
# several measurement intervals, not just reading jitter.
MAXFLOW_PERSISTENT_TOLERANCE = 1.0  # m³

MAXFLOW_PENDING_KEY = "maxFlowPendingReadings"

# --- Meter replacement detection ------------------------------------------
#
# When a water meter is physically replaced, the reading starts again at
# (close to) 0, while the existing history is at e.g. several hundred m³.
# To negativeDeltaDetector() this looks exactly like a single faulty
# outlier (which it is meant to filter out) -- without a countermeasure it
# would discard the new, correct meter reading forever and keep repeating
# the last (old) value instead.
#
# A real meter replacement differs from a one-off detection error in two
# ways:
#   1. Size: the jump is orders of magnitude larger than normal OCR noise.
#   2. Persistence: a detection error is typically a one-off -- the next
#      reading is close to the old meter value again. A real replacement
#      stays low permanently.
#
# This function only detects the *suspicion* automatically (criteria 1+2)
# and notifies the user via notification + email. The new, low meter value
# is deliberately NOT adopted automatically -- the user has to confirm it
# explicitly in the settings (see restapi.py: /meterreplacement/confirm),
# so that false alarms (e.g. a gross but one-off OCR error) are not
# accidentally turned into a permanent offset reset.
#
# The consumption reports in db.py (getConPerYear/Month/Day) read the same
# MeterReplacement table (see confirmMeterReplacement below) and split a
# period containing a replacement at that point in time instead of
# reporting a large negative "consumption".

# A downward jump larger than this must be a meter replacement and can no
# longer be a normal detection error.
METERREPLACEMENT_JUMP_THRESHOLD = 1.0  # m³

# This many consecutive raw readings have to stay consistently close to
# the new (low) value before the suspicion is reported -- protects against
# false alarms from a single gross OCR outlier.
METERREPLACEMENT_CONFIRM_COUNT = 3

# Tolerance within which consecutive raw readings still count as
# "consistently close to the new meter value" (covers normal OCR noise as
# well as a little real consumption on the new meter).
METERREPLACEMENT_CONSISTENCY_TOLERANCE = 1.0  # m³

METERREPLACEMENT_PENDING_KEY = "meterReplacementPendingReadings"


def confirmMeterReplacement() -> None:
    """
    User-triggered confirmation that the water meter was replaced (see
    restapi.py). Creates a MeterReplacement entry with the current time:
    from now on all consistency checks in this module ignore readings from
    before this point, so the new, low meter value is no longer discarded
    as an outlier against the old history. The same table is used by
    db.getConPerYear/Month/Day to split the consumption calculation at the
    replacement time instead of reporting a large negative "consumption".
    """
    addMeterReplacement(datetime.now())
    setKeyValueStoreValue(METERREPLACEMENT_PENDING_KEY, "")


def _readingsSinceLastMeterReplacement(x: int):
    """
    Like get_last_readings(x), but limited to readings since the most
    recently confirmed meter replacement (if one was confirmed). Before the
    first confirmation (or without enough history after it) falls back to
    the unrestricted list, so the filters don't misbehave for lack of data
    right after a confirmation.
    """
    readings = list(get_last_readings(x))
    confirmedAtDt = getLastMeterReplacement()
    if confirmedAtDt is None:
        return readings
    filteredReadings = [r for r in readings if r.time >= confirmedAtDt]
    return filteredReadings if filteredReadings else readings


def meterReplacementDetector(consumption: float) -> None:
    """
    Watches raw (unfiltered) readings for the pattern of a meter
    replacement (large, sustained downward jump) and reports the suspicion
    via notification + email once it is confirmed over several consecutive
    readings. Does not touch the filter pipeline value --
    negativeDeltaDetector() keeps holding the old value until the user
    confirms the replacement manually (see confirmMeterReplacement()).
    """
    readings = get_last_readings(10)
    if len(readings) == 0:
        return
    readings = sorted(readings, key=lambda x: x.time)
    lastFiltered = readings[-1].filtered

    jump = lastFiltered - consumption
    if jump < METERREPLACEMENT_JUMP_THRESHOLD:
        # No unusually large downward jump -- discard any ongoing
        # observation, since the chain is broken.
        setKeyValueStoreValue(METERREPLACEMENT_PENDING_KEY, "")
        return

    pending = getKeyValueStoreValue(METERREPLACEMENT_PENDING_KEY, default="")
    pendingValues = [float(v) for v in pending.split(",") if v != ""]

    if pendingValues and abs(consumption - pendingValues[-1]) > METERREPLACEMENT_CONSISTENCY_TOLERANCE:
        # New value deviates too much from the last suspected value --
        # probably more noise rather than a stable new meter value.
        # Restart the observation.
        pendingValues = []

    pendingValues.append(consumption)

    if len(pendingValues) >= METERREPLACEMENT_CONFIRM_COUNT:
        message = (
            f"Moeglicher Zaehlertausch erkannt: Zaehlerstand fiel von "
            f"{lastFiltered:.3f} m³ auf ~{consumption:.3f} m³ und blieb dort "
            f"ueber {len(pendingValues)} Messungen stabil. Falls der Zaehler "
            f"tatsaechlich getauscht wurde, bitte in den Einstellungen "
            f"bestaetigen, damit der neue Zaehlerstand uebernommen wird."
        )
        logger.logger.info(message)
        addNotification(message, "meterreplacementdetected", "warning")
        sendEmail("WatermeterAI Zaehlertausch erkannt", message)
        setKeyValueStoreValue(METERREPLACEMENT_PENDING_KEY, "")
    else:
        setKeyValueStoreValue(METERREPLACEMENT_PENDING_KEY, ",".join(str(v) for v in pendingValues))

# --- Missing needle/digit box detection -----------------------------------
#
# getIntegerFromPredictions() (readTotalConsumption.py) simply concatenates
# all boxes YOLO detected in an image into a digit sequence. If a box is
# not detected in a run (e.g. a needle poorly lit/partially covered,
# confidence below the conf threshold in
# predictNeedlesAndCounterArea/predictDigits), a whole digit is missing --
# the remaining digits shift by an order of magnitude and the resulting
# value jumps by a factor of 10 instead of being only slightly off. This
# cannot be detected from the digit sequence itself, only from the number
# of boxes.
#
# The water meter has a constant number of needles/digits, deliberately
# not hard-coded here (it depends on the meter model/camera crop) -- so the
# expected count is learned from the history: the most frequent value
# (mode) among the most recent successful detections. A current detection
# with fewer boxes than this learned mode is considered suspicious.
NEEDLE_DIGIT_COUNT_LOOKBACK = 40

# This many readings with a box count have to be in the history before
# the learned mode is considered reliable -- prevents false alarms right
# after a fresh start (little/no history) or while the history itself is
# still inconsistent.
NEEDLE_DIGIT_COUNT_MIN_HISTORY = 10

# Feature flag: when set to False, missingNeedleOrDigitDetector() is a pure
# no-op (always returns None without querying the history or notifying) --
# e.g. if the detector causes too many false positives in practice.
# Deliberately only disabled rather than removed, so it can be re-armed by
# changing this one line without rewriting the logic. Since it was
# enabled, the raw value a discarded reading would have produced is stored
# as well (see db.Reading.beforeMissingNeedleCheck/afterMissingNeedleCheck
# and readTotalConsumption.py:_gettotalconsumption), so a discard stays
# traceable afterwards instead of the value vanishing without a trace.
MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED = True


def _learnExpectedBoxCount(fieldName: str) -> int | None:
    """
    Determines the expected number of detected boxes (needles or digits)
    as the mode of the last NEEDLE_DIGIT_COUNT_LOOKBACK successful readings.

    Parameters:
    fieldName (str): "nNeedlesDetected" or "nDigitsDetected".

    Returns:
    int|None: learned expected count, or None if there is not enough
        history with this field (e.g. fresh start, or the DB predates
        these columns).
    """
    readings = _readingsSinceLastMeterReplacement(NEEDLE_DIGIT_COUNT_LOOKBACK)
    counts = [getattr(r, fieldName) for r in readings if getattr(r, fieldName) is not None]
    if len(counts) < NEEDLE_DIGIT_COUNT_MIN_HISTORY:
        return None
    values, occurrences = np.unique(counts, return_counts=True)
    return int(values[np.argmax(occurrences)])


def missingNeedleOrDigitDetector(nNeedlesDetected: int, nDigitsDetected: int) -> str | None:
    """
    Compares the number of needle/digit boxes detected in the current
    reading against the expected count learned from the history (see the
    module comment above). Must be called BEFORE totalconsumption is
    computed (see readTotalConsumption.py), so a suspicious reading can be
    discarded entirely instead of feeding a number shifted by an order of
    magnitude into the filter pipeline. Logs by itself (like
    meterReplacementDetector), so the caller only has to evaluate the
    result.

    Parameters:
    nNeedlesDetected (int): number of needle boxes detected in this image.
    nDigitsDetected (int): number of digit boxes detected in this image.

    Returns:
    str|None: the discard reason as ready-made text (stored by
        readTotalConsumption.py in Reading.discardReason, so developer mode
        in the frontend can show WHY a reading was discarded -- see
        CardLastPhoto.jsx), or None if the count(s) are plausible, there is
        not enough history to learn from yet, or the detector is disabled
        via MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED.
    """
    if not MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED:
        return None

    problems = []
    for label, detected, fieldName in (
        ("needles", nNeedlesDetected, "nNeedlesDetected"),
        ("digits", nDigitsDetected, "nDigitsDetected"),
    ):
        expected = _learnExpectedBoxCount(fieldName)
        if expected is not None and detected < expected:
            problems.append(
                f"{label}: expected {expected} (learned from history), got {detected}"
            )

    if not problems:
        return None

    reason = "; ".join(problems)
    message = (
        f"Messung verworfen, vermutlich fehlende Nadel-/Digit-Box: {reason}. "
        f"Ohne diese Erkennung waere die Ziffernfolge um eine Zehnerpotenz "
        f"verschoben in die Auswertung eingegangen."
    )
    # Deliberately NO addNotification() here: the frontend shows every
    # notification unfiltered in the bell dropdown at the top right (see
    # DropdownNotification.jsx), which should be reserved for when
    # something really important happens (suspected meter replacement,
    # leakage) -- not for every detection error this purely defensive
    # filter catches internally. The log remains for diagnosis; the reason
    # is also passed to the DB via the return value (see docstring above).
    logger.logger.warning(message)
    return message


def getMaxValues ():
    data = _readingsSinceLastMeterReplacement(40)
    if len(data) == 0:
        return 0, 0
    maxValue = max(data, key=lambda x: x.filtered)
    ndigitsMaxValue = len(str(int(abs(maxValue.filtered))))
    return maxValue.filtered, ndigitsMaxValue


def missingDigitDetector(consumption: float) -> float:
    maxValue, ndigitsMaxValue = getMaxValues()
    nDigits = len(str(int(abs(consumption))))
    if nDigits < ndigitsMaxValue:
        return round(int(maxValue) + (consumption - int(consumption)),4)
    else:
        return consumption

def maxFlowDetector(consumption: float) -> float:
    readings = _readingsSinceLastMeterReplacement(10)
    if len(readings) == 0:
        # No history available (e.g. freshly created/emptied DB) -- nothing
        # to compare against, pass the value through unchanged. Without this
        # guard readings[-1] below raises IndexError and aborts the whole
        # readTotalConsumption run before store_reading() (see git
        # history/PR discussion).
        return consumption
    readings = sorted(readings, key=lambda x: x.time)
    dy = abs(readings[-1].filtered - consumption)
    dt = (datetime.now() - readings[-1].time).total_seconds() / 60

    if dt == 0:
        return consumption
    # Two independent criteria, see MAXFLOW_ABSOLUTE_JUMP: the rate check
    # (dy/dt) alone lets even a very large dy through when dt is large
    # (several discarded readings in between).
    if dy/dt > MAXFLOW or dy > MAXFLOW_ABSOLUTE_JUMP:
        # Before clamping: check whether a sustained trend is already being
        # confirmed here (see MAXFLOW_PERSISTENT_CONFIRM_COUNT above) --
        # otherwise a real burst pipe would never become visible, because
        # every new (real, high) reading would again be flagged as an
        # "outlier" against the same frozen old value.
        pending = getKeyValueStoreValue(MAXFLOW_PENDING_KEY, default="")
        pendingValues = [float(v) for v in pending.split(",") if v != ""]

        if pendingValues and abs(consumption - pendingValues[-1]) > MAXFLOW_PERSISTENT_TOLERANCE:
            # Deviates too much from the last suspected value -- not a
            # consistent trend but probably more noise. Restart the
            # observation.
            pendingValues = []

        pendingValues.append(consumption)

        if len(pendingValues) >= MAXFLOW_PERSISTENT_CONFIRM_COUNT:
            # Several consecutive readings confirm the same (rising)
            # trend -- no longer a single error. Pass the value through so
            # detectLeakage() sees the actual increase, and notify the user
            # right away (same pattern as a leak alarm; a sustained flow of
            # this magnitude is exactly that by definition).
            message = (
                f"Ungewoehnlich hoher, anhaltender Wasserfluss erkannt: "
                f"Zaehlerstand stieg von {readings[-1].filtered:.3f} m³ auf "
                f"{consumption:.3f} m³ und blieb dabei ueber "
                f"{len(pendingValues)} Messungen konsistent. Moeglicher "
                f"Rohrbruch -- bitte pruefen."
            )
            logger.logger.info(message)
            addNotification(message, "highflowdetected", "warning")
            sendEmail("WatermeterAI Hoher Wasserfluss erkannt", message)
            setKeyValueStoreValue(MAXFLOW_PENDING_KEY, "")
            return consumption

        setKeyValueStoreValue(MAXFLOW_PENDING_KEY, ",".join(str(v) for v in pendingValues))

        median = np.median([x.totalconsumption for x in readings])
        # Deliberately NO addNotification() here -- see the comment in
        # missingNeedleOrDigitDetector: the bell dropdown in the frontend
        # should only show really important events (suspected meter
        # replacement, leakage), not every detection error this outlier
        # filter catches internally.
        logger.logger.info(f"Outlier detected: {readings[-1].time} {readings[-1].filtered} {consumption} {median}")
        return median

    # No outlier -- discard any ongoing observation, since the chain of
    # consistent outliers is broken.
    setKeyValueStoreValue(MAXFLOW_PENDING_KEY, "")
    return consumption

# --- negativeDeltaDetector: decrease vs. persistent new trend -------------
#
# A water meter physically cannot go down -- so EVERY decrease is an error
# somewhere in the detection. The original threshold (0.1 m³ = 100 liters)
# let almost every decrease through unchanged on the Pi in practice: 22 of
# 24 observed negative jumps in a real measurement series were between
# -0.001 and -0.09 m³, i.e. below the threshold (see chat analysis) -- an
# oscillation observed at night between e.g. 85.4389/85.4489/85.489 would
# never have been noticed.
#
# NEGDELTATHRESHOLD was therefore lowered to a pure rounding tolerance
# (floats from round(..., 5) in readTotalConsumption.py) -- but a simple
# "every decrease is discarded" would itself be dangerous: if a SINGLE
# reading comes out wrongly too HIGH (e.g. a YOLO misclassification with +1
# in one digit) and the meter then settles back on its real, lower course,
# all following -- actually correct -- readings would permanently look
# like a "decrease" relative to the wrongly high reference value and would
# be blocked forever.
#
# Solution: the same persistence idea as in meterReplacementDetector, but
# generalized to any decrease (not just the >1.0 m³ meter replacement
# special case), and this time it actually affects the filtered value (not
# just a notification):
#   - An isolated decrease (the next reading is close to the old reference
#     value again) is a one-off detection error -> discarded, the old value
#     is kept.
#   - A decrease that stays consistent over several consecutive readings is
#     no longer a coincidence -- the OLD reference value was the error (e.g.
#     the high outlier described above), and the new, lower trend is
#     adopted once confirmed.
# During the waiting phase (pending count not yet reached) the returned
# value deliberately stays frozen at the old level -- 'filtered' values
# already stored during this waiting phase are not corrected
# retroactively, the new trend only applies from confirmation onwards.
NEGDELTATHRESHOLD = 0.0005  # m³, pure rounding tolerance

# This many consecutive raw readings have to stay consistently below the
# previous reference value before the new, lower trend is considered real
# instead of being discarded as a one-off outlier.
NEGATIVE_DELTA_CONFIRM_COUNT = 3

# Tolerance within which consecutive raw readings still count as
# "consistently close to the new, lower level" (covers normal OCR noise as
# well as a little real consumption at the new level).
NEGATIVE_DELTA_CONSISTENCY_TOLERANCE = 0.05  # m³

NEGATIVE_DELTA_PENDING_KEY = "negativeDeltaPendingReadings"


def negativeDeltaDetector(consumption: float) -> float:
    readings = _readingsSinceLastMeterReplacement(10)
    if len(readings) == 0:
        # See the comment in maxFlowDetector() -- no history, nothing to
        # compare against.
        return consumption
    readings = sorted(readings, key=lambda x: x.time)
    median = np.median([x.filtered for x in readings])

    if consumption >= (median - NEGDELTATHRESHOLD):
        # No decrease (or within the rounding tolerance) -- discard any
        # ongoing observation, since the chain is broken, and pass the
        # value through unchanged.
        setKeyValueStoreValue(NEGATIVE_DELTA_PENDING_KEY, "")
        return consumption

    pending = getKeyValueStoreValue(NEGATIVE_DELTA_PENDING_KEY, default="")
    pendingValues = [float(v) for v in pending.split(",") if v != ""]

    if pendingValues and abs(consumption - pendingValues[-1]) > NEGATIVE_DELTA_CONSISTENCY_TOLERANCE:
        # New value deviates too much from the last suspected value --
        # probably more noise rather than a consistent new level. Restart
        # the observation.
        pendingValues = []

    pendingValues.append(consumption)

    if len(pendingValues) >= NEGATIVE_DELTA_CONFIRM_COUNT:
        # The decrease was confirmed over several readings -- the OLD
        # reference value was the error, not this reading. Adopt the new
        # trend from now on.
        logger.logger.info(
            f"Bestaetigter neuer, niedrigerer Trend nach {len(pendingValues)} "
            f"konsistenten Messungen: bisheriger Median {median:.4f} m³, "
            f"neuer Stand ~{consumption:.4f} m³."
        )
        setKeyValueStoreValue(NEGATIVE_DELTA_PENDING_KEY, "")
        return consumption

    setKeyValueStoreValue(NEGATIVE_DELTA_PENDING_KEY, ",".join(str(v) for v in pendingValues))
    return readings[-1].filtered


if __name__ == "__main__":
    pass
    # print(filterMissingDigit(1.234))
    # maxFlowDetector(815)
