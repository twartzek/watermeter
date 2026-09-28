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
    Ein einzelner Schritt der Ausreisser-Filter-Pipeline in
    readTotalConsumption.py (missingDigitDetector -> maxFlowDetector ->
    negativeDeltaDetector). `changed` ist True, wenn dieser Filter den Wert
    tatsaechlich veraendert hat -- so kann der Entwicklermodus im Frontend
    (CardLastPhoto.jsx) anzeigen, WELCHER Filter eingegriffen hat, statt nur
    den Wert vor/nach der gesamten Kette zu zeigen.
    """
    name: str
    before: float | None
    after: float | None
    changed: bool


class DebugInfo(BaseModel):
    """
    Zusatzinformationen fuer den Entwicklermodus (siehe CardLastPhoto.jsx):
    der Rohwert direkt aus den YOLO-Boxen (vor jedem Filter/Check), der
    Verwurfsgrund falls missingNeedleOrDigitDetector die Messung verworfen
    hat, sowie pro Filter-Schritt, ob und wie er den Wert veraendert hat.
    """
    nNeedlesDetected: int | None = None
    nDigitsDetected: int | None = None
    rawValue: float | None = None
    discardReason: str | None = None
    filterSteps: list[FilterStep] = []


class PipelineStages(BaseModel):
    """
    Alle in der DB gespeicherten Zwischenwerte EINER Messung, in
    Pipeline-Reihenfolge (siehe readTotalConsumption.py:_gettotalconsumption
    und db.Reading fuer die Herleitung jeder einzelnen Stufe). Anders als
    FilterStep (Vorher/Nachher-PAARE pro Schritt, fuer die textuelle
    Debug-Ansicht in CardLastPhoto.jsx) ist das hier ein benannter Wert PRO
    Stufe -- gedacht, um im Entwicklermodus als eigene, waehlbare Linie im
    Zeitverlauf-Chart darzustellen (siehe TableOne.jsx), nicht um einen
    einzelnen Filterschritt zu erklaeren.

    Bei einer verworfenen Messung (missingNeedleOrDigitDetector) bleiben
    afterMissingDigit/afterMaxFlow/afterNegativeDelta None, da diese drei
    Filter fuer ein verworfenes Reading nie liefen (siehe db.Reading).
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
    # Nur im Entwicklermodus gefuellt (siehe getReadings) -- alle
    # Pipeline-Zwischenwerte dieser Messung als eigene, waehlbare
    # Chart-Linien, siehe PipelineStages.
    pipelineStages: PipelineStages | None = None
    # True, wenn dieses Foto noch gar keine Auswertung hat, WEIL sie noch
    # laeuft (juenger als MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS) --
    # unterscheidet den Entwicklermodus-Anzeigezweig "Messung laeuft noch"
    # von "Erkennung ist tatsaechlich fehlgeschlagen" (totalconsumption ist
    # in beiden Faellen None, aber nur letzteres ist ein abgeschlossenes
    # Ergebnis mit Debug-Infos). Siehe CardLastPhoto.jsx.
    measurementInProgress: bool = False
    # True, wenn ein Foto existiert, das schon zu alt fuer "Messung laeuft
    # noch" ist (siehe measurementInProgress), aber trotzdem KEIN DB-Reading
    # hat -- d.h. readTotalConsumption.py ist mit einer Exception
    # abgestuerzt oder wurde durch einen Systemneustart (z.B. der
    # Datei-Watchdog aus /etc/watchdog.conf, der bei einer haengenden
    # YOLO-Inferenz den Pi hart neu startet) mitten in der Auswertung
    # abgebrochen, BEVOR store_reading() erreicht wurde. Unterscheidet
    # diesen Fall im Entwicklermodus von einem abgeschlossenen Verwurf
    # durch missingNeedleOrDigitDetector (dort existiert ein DB-Reading mit
    # discardReason gesetzt).
    measurementCrashed: bool = False


# Ein Foto, fuer das noch kein Reading existiert, gilt bis zu diesem Alter
# als "Messung laeuft noch" statt als "Erkennung fehlgeschlagen" -- ein
# einzelner readTotalConsumption.py-Lauf braucht auf dem Pi Zero 2 W
# ueblicherweise ca. 3-5 Minuten (zwei YOLO-predict()-Aufrufe, siehe
# YOLO_PREDICT_TIMEOUT_SECONDS in readTotalConsumption.py fuer den
# deutlich hoeher liegenden Notfall-Timeout), 10 Minuten geben dem
# ausreichend Luft unter normaler Last, ohne einen wirklich haengenden/
# abgestuerzten Lauf dauerhaft als "laeuft noch" zu verschleiern.
MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS = 600


# Ab diesem Alter gilt ein Reading in /readings/lastsuccessful als veraltet
# (isStale=True) -- bei einem 15-Minuten-Messintervall entspricht das etwa
# 3-4 ausgebliebenen Messungen in Folge. Toleriert damit einzelne Ausreisser
# (ein fehlgeschlagener Messversuch, ein kurzer Watchdog-Reboot mitten in
# der Inferenz), macht aber sichtbar, wenn die Erkennung laenger als das
# steht -- ohne dieses Flag wuerde ein beliebig alter letzter Erfolgswert
# (z.B. von vor Tagen) unveraendert als aktueller Wert angezeigt.
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
    Baut die Schritt-fuer-Schritt-Kette der Ausreisser-Filter-Pipeline
    (siehe readTotalConsumption.py: missingDigitDetector -> maxFlowDetector
    -> negativeDeltaDetector), inklusive ob der jeweilige Schritt den Wert
    tatsaechlich veraendert hat.
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
    """Zusatzinfo fuer den Entwicklermodus aus einem DB-Reading, siehe DebugInfo."""
    return DebugInfo(
        nNeedlesDetected=reading.nNeedlesDetected,
        nDigitsDetected=reading.nDigitsDetected,
        rawValue=reading.beforeMissingNeedleCheck,
        discardReason=reading.discardReason,
        filterSteps=_buildFilterSteps(reading),
    )


def _buildPipelineStages(reading) -> PipelineStages:
    """Alle Pipeline-Zwischenwerte eines DB-Readings, siehe PipelineStages."""
    return PipelineStages(
        rawYolo=reading.beforeMissingNeedleCheck,
        afterMissingDigit=reading.afterMissingDigit,
        afterMaxFlow=reading.afterMaxFlow,
        afterNegativeDelta=reading.afterNegativeDelta,
    )


def _imageUrlFor(imageName: str) -> str:
    """
    Liefert die URL des Bbox-annotierten Fotos (mit eingezeichneten
    Bounding-Boxen), falls es existiert, sonst die des unbearbeiteten
    Originalfotos.

    Die _bbox.jpg wird gespeichert, sobald mindestens eine Nadel- UND eine
    Digit-Box erkannt wurde (siehe readTotalConsumption.py:
    _gettotalconsumption) -- also auch dann, wenn missingNeedleOrDigitDetector
    die Messung anschliessend verwirft (totalconsumption=None): der
    Entwicklermodus soll gerade in diesem Fall die erkannten Boxen sehen
    koennen, um nachzuvollziehen, welche Nadel/Ziffer gefehlt hat. Nur wenn
    ueberhaupt keine Boxen erkannt wurden (totalconsumption war schon vor
    dem missingNeedleOrDigitDetector-Check None), existiert die Datei
    nicht und das Original wird gezeigt.
    """
    bboxImageName = imageName + "_bbox.jpg"
    if os.path.isfile(os.path.join(config["images"], bboxImageName)):
        return app.url_path_for("images", path=bboxImageName)
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
                # Ein readTotalConsumption.py-Lauf braucht ueblicherweise
                # mehrere Minuten (siehe MEASUREMENT_IN_PROGRESS_THRESHOLD_
                # SECONDS) -- ein frisches Foto ohne Reading ist meistens
                # einfach eine noch laufende Messung, kein tatsaechlicher
                # Fehlschlag. Ohne diese Unterscheidung zeigt der
                # Entwicklermodus faelschlich "Erkennung fehlgeschlagen"
                # fuer die gesamte Auswertungsdauer, obwohl noch gar kein
                # Ergebnis vorliegt.
                age_seconds = (datetime.now() - takenAt).total_seconds()
                inProgress = age_seconds < MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS
                return Reading(
                    id=None,
                    datetime=takenAt,
                    totalconsumption=None,
                    imageUrl=app.url_path_for("images", path=imageName),
                    measurementInProgress=inProgress,
                    # Aelter als die uebliche Auswertungsdauer und immer
                    # noch kein DB-Reading -> der Lauf ist abgestuerzt oder
                    # wurde abgebrochen (z.B. durch einen Watchdog-Reboot
                    # mitten in der YOLO-Inferenz), nicht "laeuft noch".
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
    Wie /readings/last, aber immer nur der letzte *erfolgreiche* Wert --
    unabhaengig vom Entwicklermodus. Fuer Status-Kacheln (aktueller
    Zaehlerstand, Gesamtverbrauch): ein einzelner fehlgeschlagener
    Messversuch (z.B. durch einen Watchdog-Reboot mitten in der Inferenz,
    siehe git history/PR-Diskussion) soll dort nicht "Keine Daten" zeigen,
    solange zuvor ein gueltiger Wert vorlag -- der Zaehlerstand aendert
    sich schliesslich nicht dadurch, dass ein Foto nicht ausgewertet werden
    konnte.
    Damit ein beliebig alter Wert (z.B. von vor Tagen, falls die Erkennung
    laenger ausfaellt) nicht unbemerkt als aktuell durchgeht, liefert dieser
    Endpunkt zusaetzlich isStale=True, sobald der Wert aelter als
    STALE_READING_THRESHOLD_MINUTES ist -- das Frontend markiert das
    entsprechend sichtbar, statt den Wert kommentarlos anzuzeigen.
    /readings/last bleibt unveraendert fuer den Entwicklermodus-Fallback
    (Foto-Anzeige auch ohne DB-Eintrag), siehe CardLastPhoto.jsx.
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
        imageUrl=app.url_path_for("images", path=imageName),
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
        reading.imageUrl = app.url_path_for("images", path=imageName)
        readings.append(Reading(id=reading.id, datetime=reading.time, totalconsumption=reading.totalconsumption, imageUrl=app.url_path_for("images", path=imageName), filteredTotal=reading.filtered))
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
        reading.imageUrl = app.url_path_for("images", path=imageName)
        readings.append(Reading(
            id=reading.id,
            datetime=reading.time,
            totalconsumption=reading.totalconsumption,
            imageUrl=app.url_path_for("images", path=imageName),
            filteredTotal=reading.filtered,
            # Nur im Entwicklermodus befuellt: die vollen Pipeline-Zwischen-
            # werte sind fuer den normalen Betrieb unnoetiger Payload-
            # Overhead auf einem Endpunkt, der pro Chart-Refresh mehrere
            # Tage Readings auf einmal laedt (siehe TableOne.jsx).
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
    Loescht ALLE Notifications unwiderruflich (Entwicklermodus, siehe
    SettingsPanel.jsx) -- z.B. um alte Fehlalarme aus der Glocken-
    Dropdown-Liste zu entfernen, ohne jede einzeln manuell abzuhaken.
    """
    deleteAllNotifications()
    return {"ok": True}

@app.get("/api/v1/meterreplacements")
def getMeterReplacementsApi() -> list[MeterReplacementOut]:
    """
    Alle bestaetigten Zaehlertausch-Zeitpunkte, aelteste zuerst. Fuers
    Frontend, um Zeitreihen-Charts am Tauschzeitpunkt zu annotieren (siehe
    ChartOne.jsx) -- ohne diesen Hinweis sieht ein Sprung im Verlauf wie ein
    Datenfehler statt wie ein neuer Zaehler aus.
    """
    return [MeterReplacementOut(time=t) for t in getMeterReplacements()]

@app.post("/api/v1/meterreplacement/confirm")
def confirmMeterReplacementApi():
    """
    Vom Nutzer ausgeloest, nachdem der Wasserzaehler tatsaechlich getauscht
    wurde (typischerweise nach der "Moeglicher Zaehlertausch erkannt"
    Notification, siehe outlierDetection.meterReplacementDetector). Ab
    sofort wird der neue, niedrige Zaehlerstand akzeptiert statt gegen die
    History des alten Zaehlers verworfen zu werden.
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
        # 15 statt 10 Minuten: ein Messlauf kann auf dem Pi Zero 2 W unter
        # Speicherdruck deutlich laenger als 10 Minuten dauern (siehe
        # YOLO_PREDICT_TIMEOUT_SECONDS in readTotalConsumption.py und den
        # flock-Schutz in startMeasurement.sh). Bei */10 wuerde der naechste
        # Cron-Trigger dann regelmaessig auf einen noch laufenden Vorgaenger
        # treffen und per flock uebersprungen -- die effektive Messfrequenz
        # waere dadurch unvorhersehbar niedriger als die eingestellten 10
        # Minuten suggerieren. 15 Minuten geben jedem Lauf realistisch genug
        # Luft, bevor der naechste startet.
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