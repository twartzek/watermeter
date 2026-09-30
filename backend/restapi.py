from typing import Union
from pydantic import BaseModel
from datetime import datetime
import os
import uvicorn

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import platform
from dotenv import dotenv_values
from settingshandler import readSettings, writeSettings
from emailhandler import sendEmail

config = dotenv_values("watermeter/.env")  

try:
    from crontab import CronTab
except ImportError:
    pass

from db import get_last_readings, get_newest_image, create_db_if_not_exists, get_readings_between, getConPerMonth, getConPerYear, getConPerDay, getConsumptionBetween, deleteReading, getAllNotifications, updateViewedNotifications, deleteAllNotifications, getCumulativeTotal, getMeterReplacements
from outlierDetection import confirmMeterReplacement


def is_raspberry_pi():
    return (
        platform.machine() == "armv7l"
        or platform.machine() == "armv6l"
        or platform.machine() == "aarch64"
    )

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.mount("/api/v1/images", StaticFiles(directory=config["images"]), name="images")
# app.mount("/", StaticFiles(directory=config["frontend"], html=True), name="frontend")


class FilterStep(BaseModel):
    """
    A single step of the outlier filter pipeline in readTotalConsumption.py
    (missingDigitDetector -> maxFlowDetector -> negativeDeltaDetector).
    `changed` is True if this filter actually changed the value -- so
    developer mode in the frontend (CardLastPhoto.jsx) can show WHICH filter
    intervened, instead of only the value before/after the whole chain.
    """
    name: str
    before: float | None
    after: float | None
    changed: bool


class DebugInfo(BaseModel):
    """
    Additional information for developer mode (see CardLastPhoto.jsx): the
    raw value straight from the YOLO boxes (before any filter/check), the
    discard reason if missingNeedleOrDigitDetector discarded the reading,
    and for each filter step whether and how it changed the value.
    """
    nNeedlesDetected: int | None = None
    nDigitsDetected: int | None = None
    rawValue: float | None = None
    discardReason: str | None = None
    filterSteps: list[FilterStep] = []


class PipelineStages(BaseModel):
    """
    All intermediate values of ONE reading stored in the DB, in pipeline
    order (see readTotalConsumption.py:_gettotalconsumption and db.Reading
    for how each stage is derived). Unlike FilterStep (before/after PAIRS
    per step, for the textual debug view in CardLastPhoto.jsx), this is one
    named value PER stage -- meant to be shown in developer mode as its own
    selectable line in the time series chart (see TableOne.jsx), not to
    explain a single filter step.

    For a discarded reading (missingNeedleOrDigitDetector),
    afterMissingDigit/afterMaxFlow/afterNegativeDelta stay None, since these
    three filters never ran for a discarded reading (see db.Reading).
    """
    rawYolo: float | None = None
    afterMissingDigit: float | None = None
    afterMaxFlow: float | None = None
    afterNegativeDelta: float | None = None


class Reading(BaseModel):
    id: int | None
    datetime: datetime | None
    totalconsumption: float | None
    imageUrl: str | None
    filteredTotal: float | None = None
    cumulativeTotal: float | None = None
    debugInfo: DebugInfo | None = None
    # Only filled in developer mode (see getReadings) -- all pipeline
    # intermediate values of this reading as separate, selectable chart
    # lines, see PipelineStages.
    pipelineStages: PipelineStages | None = None
    # True if this photo has no evaluation yet BECAUSE it is still running
    # (younger than MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS) --
    # distinguishes the developer mode display branch "measurement still
    # running" from "detection actually failed" (totalconsumption is None in
    # both cases, but only the latter is a finished result with debug info).
    # See CardLastPhoto.jsx.
    measurementInProgress: bool = False
    # True if a photo exists that is already too old for "measurement still
    # running" (see measurementInProgress) but still has NO DB reading --
    # i.e. readTotalConsumption.py crashed with an exception or was aborted
    # mid-evaluation by a system restart (e.g. the file watchdog from
    # /etc/watchdog.conf, which hard-reboots the Pi on a hanging YOLO
    # inference) BEFORE store_reading() was reached. Distinguishes this case
    # in developer mode from a completed discard by
    # missingNeedleOrDigitDetector (where a DB reading with discardReason
    # set exists).
    measurementCrashed: bool = False


# A photo without a reading yet counts as "measurement still running"
# rather than "detection failed" up to this age -- a single
# readTotalConsumption.py run usually takes about 3-5 minutes on the Pi
# Zero 2 W (two YOLO predict() calls, see YOLO_PREDICT_TIMEOUT_SECONDS in
# readTotalConsumption.py for the much higher emergency timeout); 10
# minutes gives that enough headroom under normal load without permanently
# disguising a really hanging/crashed run as "still running".
MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS = 600


# From this age on, a reading in /readings/lastsuccessful counts as stale
# (isStale=True) -- with a 15-minute measurement interval that is about 3-4
# missed readings in a row. This tolerates individual outliers (one failed
# measurement attempt, a short watchdog reboot mid-inference) but makes it
# visible when detection is down for longer than that -- without this flag
# an arbitrarily old last successful value (e.g. from days ago) would be
# shown unchanged as the current value.
STALE_READING_THRESHOLD_MINUTES = 60


class ReadingWithStaleness(Reading):
    isStale: bool = False

class Consumption(BaseModel):
    datetime: datetime
    consumption: float | None
    

class ConsumptionPerYear(BaseModel):
    year: int
    consumption: float | None
    rate: float | None

class ConsumptionPerMonth(BaseModel):
    yearmonth: str
    consumption: float | None
    rate: float | None

class ConsumptionPerDay(BaseModel):
    day: int
    consumption: float | None
    rate: float | None

class ConsumptionBetween(BaseModel):
    first: float | None
    last: float | None
    consumption: float | None

class Mqtt(BaseModel):
    broker: str
    port: int
    username: str
    password: str

class Smtp(BaseModel):
    server: str
    port: int
    sender: str
    recipient: str
    password: str

class Settings(BaseModel):
    mqtt: Mqtt
    smtp: Smtp
    developerMode: bool = False

class Notification(BaseModel):
    id: int
    time: datetime
    message: str
    i18nIdentifier: str
    type: str
    viewed: bool

class MeterReplacementOut(BaseModel):
    time: datetime


@app.get("/api/v1")
def root():
    return {"Hello": "World"}


def _buildFilterSteps(reading) -> list[FilterStep]:
    """
    Builds the step-by-step chain of the outlier filter pipeline (see
    readTotalConsumption.py: missingDigitDetector -> maxFlowDetector ->
    negativeDeltaDetector), including whether each step actually changed
    the value.
    """
    steps = []

    pipeline_steps_raw = [
        ("missingDigitDetector", reading.totalconsumption, reading.afterMissingDigit),
        ("maxFlowDetector", reading.afterMissingDigit, reading.afterMaxFlow),
        ("negativeDeltaDetector", reading.afterMaxFlow, reading.afterNegativeDelta),
    ]
    if not any(after is None for _, _, after in pipeline_steps_raw):
        steps.extend(
            FilterStep(name=name, before=before, after=after, changed=(before != after))
            for name, before, after in pipeline_steps_raw
        )
    return steps


def _buildDebugInfo(reading) -> DebugInfo:
    """Developer mode info for a DB reading, see DebugInfo."""
    return DebugInfo(
        nNeedlesDetected=reading.nNeedlesDetected,
        nDigitsDetected=reading.nDigitsDetected,
        rawValue=reading.beforeMissingNeedleCheck,
        discardReason=reading.discardReason,
        filterSteps=_buildFilterSteps(reading),
    )


def _buildPipelineStages(reading) -> PipelineStages:
    """All pipeline intermediate values of a DB reading, see PipelineStages."""
    return PipelineStages(
        rawYolo=reading.beforeMissingNeedleCheck,
        afterMissingDigit=reading.afterMissingDigit,
        afterMaxFlow=reading.afterMaxFlow,
        afterNegativeDelta=reading.afterNegativeDelta,
    )


def _imageUrlFor(imageName: str) -> str | None:
    """
    Returns the URL of the bbox-annotated photo (with the bounding boxes
    drawn in) if it exists, otherwise that of the unprocessed original
    photo (or None if that is gone too).

    The _bbox.jpg is saved as soon as at least one needle AND one digit box
    were detected (see readTotalConsumption.py:_gettotalconsumption) -- so
    also when missingNeedleOrDigitDetector discards the reading afterwards
    (totalconsumption=None): especially in that case developer mode should
    be able to see the detected boxes, to understand which needle/digit was
    missing. Only if no boxes were detected at all (totalconsumption was
    already None before the missingNeedleOrDigitDetector check) does the
    file not exist, and the original is shown.
    """
    bboxImageName = imageName + "_bbox.jpg"
    if os.path.isfile(os.path.join(config["images"], bboxImageName)):
        return app.url_path_for("images", path=bboxImageName)
    return _originalImageUrlIfExists(imageName)


def _originalImageUrlIfExists(imageName: str) -> str | None:
    """
    URL of the original photo, or None if it no longer exists: readings are
    kept at full resolution permanently, but older photos are thinned out
    nightly (see db.py:thin_out_old_images).
    """
    if not os.path.isfile(os.path.join(config["images"], imageName)):
        return None
    return app.url_path_for("images", path=imageName)


@app.get("/api/v1/readings/last")
def getLastReading() -> Reading:
    settings = readSettings()
    developerMode = settings.get("developerMode", False)
    query = get_last_readings(1, only_successful=not developerMode)
    lastReading = query[0] if len(query) > 0 else None

    if developerMode:
        # Detection can fail with an exception (not just totalconsumption
        # =None) before store_reading() is ever called, e.g. a bug or a
        # corrupt frame -- the photo is still saved to disk at that point,
        # it just has no DB row. Compare against the images directory
        # directly so developer mode still shows that photo instead of a
        # stale DB reading.
        newestImage = get_newest_image()
        if newestImage is not None:
            imageName, takenAt = newestImage
            if lastReading is None or takenAt > lastReading.time:
                # A readTotalConsumption.py run usually takes several
                # minutes (see MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS) --
                # a fresh photo without a reading is usually just a
                # measurement still in progress, not an actual failure.
                # Without this distinction developer mode would wrongly
                # show "detection failed" for the whole evaluation time,
                # although there is no result yet.
                age_seconds = (datetime.now() - takenAt).total_seconds()
                inProgress = age_seconds < MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS
                return Reading(
                    id=None,
                    datetime=takenAt,
                    totalconsumption=None,
                    imageUrl=app.url_path_for("images", path=imageName),
                    measurementInProgress=inProgress,
                    # Older than the usual evaluation time and still no DB
                    # reading -> the run crashed or was aborted (e.g. by a
                    # watchdog reboot mid-YOLO-inference), not "still
                    # running".
                    measurementCrashed=not inProgress,
                )

    if lastReading is not None:
        imageName = os.path.basename(lastReading.imageName)
        return Reading(
            id=lastReading.id,
            datetime=lastReading.time,
            totalconsumption=lastReading.totalconsumption,
            imageUrl=_imageUrlFor(imageName),
            filteredTotal=lastReading.filtered,
            cumulativeTotal=getCumulativeTotal(),
            debugInfo=_buildDebugInfo(lastReading) if developerMode else None,
        )
    else:
        return Reading(id=None, datetime=None, totalconsumption=None, imageUrl=None, filteredTotal=None, cumulativeTotal=None)


@app.get("/api/v1/readings/lastsuccessful")
def getLastSuccessfulReading() -> ReadingWithStaleness:
    """
    Like /readings/last, but always only the last *successful* value --
    regardless of developer mode. For status tiles (current meter reading,
    total consumption): a single failed measurement attempt (e.g. due to a
    watchdog reboot mid-inference, see git history/PR discussion) should not
    show "no data" there as long as there was a valid value before -- after
    all, the meter reading doesn't change just because one photo couldn't
    be evaluated.
    So that an arbitrarily old value (e.g. from days ago, if detection is
    down for longer) doesn't silently pass as current, this endpoint also
    returns isStale=True once the value is older than
    STALE_READING_THRESHOLD_MINUTES -- the frontend marks this visibly
    instead of showing the value without comment.
    /readings/last stays unchanged for the developer mode fallback (photo
    display even without a DB entry), see CardLastPhoto.jsx.
    """
    query = get_last_readings(1, only_successful=True)
    lastReading = query[0] if len(query) > 0 else None

    if lastReading is None:
        return ReadingWithStaleness(
            id=None, datetime=None, totalconsumption=None, imageUrl=None,
            filteredTotal=None, cumulativeTotal=None, isStale=False,
        )

    ageMinutes = (datetime.now() - lastReading.time).total_seconds() / 60
    imageName = os.path.basename(lastReading.imageName)
    return ReadingWithStaleness(
        id=lastReading.id,
        datetime=lastReading.time,
        totalconsumption=lastReading.totalconsumption,
        imageUrl=_originalImageUrlIfExists(imageName),
        filteredTotal=lastReading.filtered,
        cumulativeTotal=getCumulativeTotal(),
        isStale=ageMinutes > STALE_READING_THRESHOLD_MINUTES,
    )


@app.get("/api/v1/readings/{count}")
def getLastReadings(count:int) -> list[Reading]:
    settings = readSettings()
    lastReadings = get_last_readings(count, only_successful=not settings.get("developerMode", False))
    readings = []
    for reading in lastReadings:
        imageName = os.path.basename(reading.imageName)
        readings.append(Reading(id=reading.id, datetime=reading.time, totalconsumption=reading.totalconsumption, imageUrl=_originalImageUrlIfExists(imageName), filteredTotal=reading.filtered))
        # TODO: in last readings chart make y axis only 2 digits after comma
    return readings

@app.get("/api/v1/readings")
def getReadings(start: str, end: str) -> list[Reading]:
    settings = readSettings()
    developerMode = settings.get("developerMode", False)
    startdt = datetime.fromisoformat(start)
    enddt = datetime.fromisoformat(end)
    readingsbetween = get_readings_between(startdt, enddt, only_successful=not developerMode)
    readings = []
    for reading in readingsbetween:
        imageName = os.path.basename(reading.imageName)
        readings.append(Reading(
            id=reading.id,
            datetime=reading.time,
            totalconsumption=reading.totalconsumption,
            imageUrl=_originalImageUrlIfExists(imageName),
            filteredTotal=reading.filtered,
            # Only filled in developer mode: the full pipeline
            # intermediate values are unnecessary payload overhead in
            # normal operation on an endpoint that loads several days of
            # readings at once per chart refresh (see TableOne.jsx).
            pipelineStages=_buildPipelineStages(reading) if developerMode else None,
        ))
    return readings


@app.get("/api/v1/consumptionpermonth/")
def consumptionPerMonth(year: int) -> list[ConsumptionPerMonth]:
    query = getConPerMonth(year)
    consumptions = []
    for row in query:
        consumptions.append(ConsumptionPerMonth(yearmonth=row["year-month"], consumption=row["consumption"],rate=row["rate"]))

    return consumptions  

@app.get("/api/v1/consumptionperyear/")
def consumptionPerYear() -> list[ConsumptionPerYear]:
    query = getConPerYear()
    consumptions = []
    for row in query:
        consumptions.append(ConsumptionPerYear(year=row["year"], consumption=row["consumption"], rate=row["rate"]))

    return consumptions  

@app.get("/api/v1/consumptionperday/")
def consumptionPerDay(year: int, month: int) -> list[ConsumptionPerDay]:
    query = getConPerDay(year, month)
    consumptions = []
    for row in query:
        consumptions.append(ConsumptionPerDay(day=row["day"], consumption=row["consumption"], rate=row["rate"]))

    return consumptions

@app.get("/api/v1/consumptionbetween")
def consumptionBetween(start: str, end: str) -> ConsumptionBetween:
    """
    Consumption for an arbitrary time range (e.g. "this week", computed
    client-side), corrected for any confirmed meter replacement in that
    range -- see db.getConsumptionBetween. Used instead of computing
    last-minus-first from /api/v1/readings on the client, which doesn't
    know about meter replacements.
    """
    startdt = datetime.fromisoformat(start)
    enddt = datetime.fromisoformat(end)
    result = getConsumptionBetween(startdt, enddt)
    return ConsumptionBetween(**result)


@app.get("/api/v1/settings")
def getSettings() -> Settings:
    settings = readSettings()
    return settings  

@app.post("/api/v1/settings")
def storeSettings(settings: Settings):
    writeSettings(settings.model_dump_json())

    return settings  


@app.delete("/api/v1/readings/{id}")
def delReading(id:int):
    result = deleteReading(id)
    if not result:
        raise HTTPException(status_code=404, detail="Reading not found")
    return {"ok": True}

@app.get("/api/v1/notifications")
def getNotifications() -> list[Notification]:
    settings = getAllNotifications()
    return settings  

@app.post("/api/v1/notifications")
def updateViewedNotificationsApi(notifications: list[Notification]):
    print(notifications)
    updateViewedNotifications(notifications)
    return {"ok": True}

@app.delete("/api/v1/notifications")
def deleteAllNotificationsApi():
    """
    Deletes ALL notifications irrevocably (developer mode, see
    SettingsPanel.jsx) -- e.g. to clear old false alarms from the bell
    dropdown list without ticking off each one manually.
    """
    deleteAllNotifications()
    return {"ok": True}

@app.get("/api/v1/meterreplacements")
def getMeterReplacementsApi() -> list[MeterReplacementOut]:
    """
    All confirmed meter replacement times, oldest first. For the frontend,
    to annotate time series charts at the replacement time (see
    ChartOne.jsx) -- without this hint a jump in the series looks like a
    data error rather than a new meter.
    """
    return [MeterReplacementOut(time=t) for t in getMeterReplacements()]

@app.post("/api/v1/meterreplacement/confirm")
def confirmMeterReplacementApi():
    """
    Triggered by the user after the water meter was actually replaced
    (typically after the "possible meter replacement detected"
    notification, see outlierDetection.meterReplacementDetector). From now
    on the new, low meter reading is accepted instead of being discarded
    against the old meter's history.
    """
    confirmMeterReplacement()
    return {"ok": True}

@app.get("/api/v1/checksmtp")
def checkSmtp():
    error = sendEmail("WatermeterAI NextGen SMTP Test",  "Hurray! Your WatermeterAI NextGen SMTP Settings are working") 
    if error:
        raise HTTPException(status_code=404, detail="SMTP Settings not working")
    return {"ok": True} 

if __name__ == "__main__":
    
    if is_raspberry_pi():
        venvbinary = config["venv"] + "/bin/python"
        cron = CronTab(user="admin")
        cron.remove_all()
        job_detectLeakage = cron.new(command=f'{venvbinary} {config["mainpath"]}/backend/leakageDetector.py >> {config["log"]}/watermeter_leakdetector_cron.log 2>&1' )
        job_detectLeakage.hour.on(4)
        job_detectLeakage.minute.on(0)
        job_detectLeakage.dom.every(1)  # every day
        job_detectLeakage.month.every(1)  # every month
        job_detectLeakage.dow.every(1)  # every day of the week
        job_readmeasurement = cron.new(command=f'{config["mainpath"]}/backend/startMeasurement.sh > {config["log"]}/watermeter_readtotalconsumption_cron.log 2>&1' )
        # 15 instead of 10 minutes: a measurement run can take well over 10
        # minutes on the Pi Zero 2 W under memory pressure (see
        # YOLO_PREDICT_TIMEOUT_SECONDS in readTotalConsumption.py and the
        # flock guard in startMeasurement.sh). With */10 the next cron
        # trigger would then regularly hit a predecessor that is still
        # running and be skipped via flock -- the effective measurement
        # rate would be unpredictably lower than the configured 10 minutes
        # suggest. 15 minutes realistically gives each run enough headroom
        # before the next one starts.
        job_readmeasurement.minute.every(15)
        job_cleanoldreadings = cron.new(command=f'{venvbinary} {config["mainpath"]}/backend/cleanupdatabase.py >> {config["log"]}/watermeter_cleandb_cron.log 2>&1' )
        job_cleanoldreadings.hour.on(2)
        job_cleanoldreadings.minute.on(0)
        job_cleanoldreadings.dom.every(1)  # every day
        job_cleanoldreadings.month.every(1)  # every month
        job_cleanoldreadings.dow.every(1)  # every day of the week
        cron.write()   

    create_db_if_not_exists()
    uvicorn.run(app, host="0.0.0.0", port=8000)