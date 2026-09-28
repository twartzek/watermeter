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

# Absolute Obergrenze fuer einen einzelnen Sprung (dy), unabhaengig von der
# seit der letzten Messung vergangenen Zeit (dt). maxFlowDetector() prueft
# eigentlich eine RATE (dy/dt <= MAXFLOW) -- das allein reicht nicht: wenn
# mehrere Zwischenmessungen verworfen wurden (z.B. schlechte Beleuchtung),
# kann dt gross werden, wodurch auch ein sehr grosser dy noch unter der
# Raten-Schwelle bleibt. Real beobachtet: ein YOLO-Ablesefehler (86.9 ->
# 87.9, wahrscheinlich eine falsch gelesene Digit-Stelle) sprang um 1.02 m³
# bei dt=75min (rate=0.0136 m³/min, weit unter MAXFLOW) und wurde dadurch
# akzeptiert, obwohl er der mit Abstand groesste jemals akzeptierte Sprung
# in der gesamten History war (naechstgroesster: 0.23 m³). Dieser Floor
# faengt genau diesen Fall, ohne die Raten-Pruefung fuer normale, dichte
# Messintervalle zu veraendern -- 0.4 m³ liegt deutlich ueber dem groessten
# bisher beobachteten echten Einzelsprung (Dusche/Waschmaschine, ~0.1-0.23
# m³), aber weit unter einem Zehnerpotenz-Ablesefehler.
MAXFLOW_ABSOLUTE_JUMP = 0.4  # m³, unabhaengig von dt

# --- Anhaltend hoher Fluss (Rohrbruch) vs. einmaliger Ausreisser ---------
#
# maxFlowDetector() vergleicht IMMER gegen readings[-1].filtered -- das
# zuletzt GESPEICHERTE (also ggf. selbst schon geklemmte) Reading. Bei
# einem einmaligen Erkennungsfehler ist das genau richtig: die naechste
# echte Messung liegt wieder nahe am alten, unveraenderten Zaehlerstand,
# der Vergleichspunkt "friert" also nur fuer einen Zyklus ein.
#
# Bei einem ECHTEN, anhaltenden Rohrbruch mit sehr hohem Fluss passiert
# dagegen etwas anderes: jede neue Messung wird gegen den (durch die
# vorherige Klemmung eingefrorenen) alten Wert verglichen, ist relativ
# dazu also IMMER "zu weit weg" -- der gespeicherte Wert bleibt ueber
# beliebig viele Zyklen hinweg beim alten Stand haengen, waehrend der
# echte Zaehler ungebremst weiterlaeuft. Ein Rohrbruch mit 40 L/min ueber
# 3h wuerde so nie sichtbar werden: filtered bliebe konstant, obwohl real
# 7.2 m³ ausgelaufen sind -- fatal, denn genau in diesem Fall (viel Wasser
# LAEUFT GERADE aus) muss detectLeakage() (leakageDetector.py) den Anstieg
# in den Rohdaten ueberhaupt erst sehen koennen, um Alarm zu schlagen.
#
# Unterscheidung wie bei der Zaehlertausch-Erkennung oben: Persistenz.
# Bleiben mehrere aufeinanderfolgende rohe Messwerte konsistent nahe
# beieinander UND oberhalb dessen, was noch nach einem Einzelfehler
# aussieht, ist das kein Rauschen mehr, sondern ein echter, anhaltender
# Trend -- ab dann wird der aktuelle Wert durchgelassen statt weiter
# geklemmt, damit die Leck-Detektoren ihn sehen.
MAXFLOW_PERSISTENT_CONFIRM_COUNT = 3

# Toleranz, innerhalb derer aufeinanderfolgende rohe Messwerte noch als
# "konsistent am selben anhaltenden Trend" gelten. Hoeher als
# METERREPLACEMENT_CONSISTENCY_TOLERANCE, weil hier (anders als bei einem
# Zaehlertausch, der auf einem neuen Niveau STEHEN bleibt) der Zaehler
# waehrend der Beobachtung selbst weiter steigt -- die Toleranz muss den
# Verbrauch mehrerer Messintervalle mit abdecken, nicht nur Ablesejitter.
MAXFLOW_PERSISTENT_TOLERANCE = 1.0  # m³

MAXFLOW_PENDING_KEY = "maxFlowPendingReadings"

# --- Zaehlertausch-Erkennung -------------------------------------------
#
# Wird ein Wasserzaehler physisch getauscht, faengt der Zaehlerstand wieder
# bei (nahe) 0 an, waehrend die bisherige History bei z.B. mehreren hundert
# m³ liegt. Fuer negativeDeltaDetector() sieht das exakt wie ein einzelner
# fehlerhafter Ausreisser aus (den er ja bewusst herausfiltern soll) --
# ohne Gegenmassnahme wuerde er den neuen, korrekten Zaehlerstand fuer immer
# verwerfen und stattdessen den letzten (alten) Wert wiederholen.
#
# Ein echter Zaehlertausch unterscheidet sich von einem einmaligen
# Erkennungsfehler durch zwei Merkmale:
#   1. Groesse: der Sprung ist um Groessenordnungen groesser als normales
#      OCR-Rauschen.
#   2. Persistenz: ein Erkennungsfehler ist typischerweise einmalig -- die
#      naechste Messung liegt wieder in der Naehe des alten Zaehlerstands.
#      Ein echter Tausch bleibt dauerhaft niedrig.
#
# Diese Funktion erkennt automatisch nur den *Verdacht* (Kriterium 1+2) und
# benachrichtigt den Nutzer per Notification + E-Mail. Der neue, niedrige
# Zaehlerstand wird bewusst NICHT automatisch uebernommen -- das muss der
# Nutzer ueber die Einstellungen (siehe restapi.py: /meterreplacement/confirm)
# explizit bestaetigen, um Fehlalarme (z.B. ein grober, aber einmaliger
# OCR-Fehler) nicht versehentlich in einen dauerhaften Offset-Reset zu
# verwandeln.
#
# Die Verbrauchsauswertungen in db.py (getConPerYear/Month/Day) lesen die
# gleiche MeterReplacement-Tabelle (siehe confirmMeterReplacement unten)
# und splitten einen Zeitraum, der einen Tausch enthaelt, an dessen
# Zeitpunkt, statt einen grossen negativen "Verbrauch" auszuweisen.

# Ein Sprung nach unten, der groesser ist als das, muss ein Zaehlertausch
# sein und kann kein normaler Erkennungsfehler mehr sein.
METERREPLACEMENT_JUMP_THRESHOLD = 1.0  # m³

# So viele aufeinanderfolgende rohe Messungen muessen konsistent nahe am
# neuen (niedrigen) Wert bleiben, bevor der Verdacht gemeldet wird --
# schuetzt vor Fehlalarm durch einen einzelnen groben OCR-Ausreisser.
METERREPLACEMENT_CONFIRM_COUNT = 3

# Toleranz, innerhalb derer aufeinanderfolgende rohe Messungen noch als
# "konsistent nahe am neuen Zaehlerstand" gelten (deckt normales
# OCR-Rauschen sowie ein wenig echten Verbrauch am neuen Zaehler ab).
METERREPLACEMENT_CONSISTENCY_TOLERANCE = 1.0  # m³

METERREPLACEMENT_PENDING_KEY = "meterReplacementPendingReadings"


def confirmMeterReplacement() -> None:
    """
    Vom Nutzer ausgeloeste Bestaetigung, dass der Wasserzaehler getauscht
    wurde (siehe restapi.py). Legt einen MeterReplacement-Eintrag mit dem
    aktuellen Zeitpunkt an: ab sofort ignorieren alle Konsistenzchecks in
    diesem Modul Readings von vor diesem Zeitpunkt, sodass der neue,
    niedrige Zaehlerstand nicht mehr als Ausreisser gegen die alte History
    verworfen wird. Dieselbe Tabelle wird von db.getConPerYear/Month/Day
    genutzt, um die Verbrauchsberechnung am Tauschzeitpunkt zu splitten,
    statt einen grossen negativen "Verbrauch" auszuweisen.
    """
    addMeterReplacement(datetime.now())
    setKeyValueStoreValue(METERREPLACEMENT_PENDING_KEY, "")


def _readingsSinceLastMeterReplacement(x: int):
    """
    Wie get_last_readings(x), aber beschraenkt auf Readings ab dem zuletzt
    bestaetigten Zaehlertausch (falls einer bestaetigt wurde). Faellt vor
    der ersten Bestaetigung (bzw. ohne genug History danach) auf die
    unbeschraenkte Liste zurueck, damit sich die Filter direkt nach einer
    Bestaetigung nicht durch Datenmangel kaputt verhalten.
    """
    readings = list(get_last_readings(x))
    confirmedAtDt = getLastMeterReplacement()
    if confirmedAtDt is None:
        return readings
    filteredReadings = [r for r in readings if r.time >= confirmedAtDt]
    return filteredReadings if filteredReadings else readings


def meterReplacementDetector(consumption: float) -> None:
    """
    Beobachtet rohe (ungefilterte) Messwerte auf das Muster eines
    Zaehlertauschs (grosser, anhaltender Sprung nach unten) und meldet den
    Verdacht per Notification + E-Mail, sobald er sich ueber mehrere
    aufeinanderfolgende Messungen bestaetigt. Greift nicht in den
    Filter-Pipeline-Wert ein -- negativeDeltaDetector() haelt den alten
    Wert weiterhin, bis der Nutzer den Tausch manuell bestaetigt (siehe
    confirmMeterReplacement()).
    """
    readings = get_last_readings(10)
    if len(readings) == 0:
        return
    readings = sorted(readings, key=lambda x: x.time)
    lastFiltered = readings[-1].filtered

    jump = lastFiltered - consumption
    if jump < METERREPLACEMENT_JUMP_THRESHOLD:
        # Kein aussergewoehnlich grosser Sprung nach unten -- laufende
        # Verdachtsbeobachtung (falls vorhanden) verwerfen, da die Kette
        # unterbrochen ist.
        setKeyValueStoreValue(METERREPLACEMENT_PENDING_KEY, "")
        return

    pending = getKeyValueStoreValue(METERREPLACEMENT_PENDING_KEY, default="")
    pendingValues = [float(v) for v in pending.split(",") if v != ""]

    if pendingValues and abs(consumption - pendingValues[-1]) > METERREPLACEMENT_CONSISTENCY_TOLERANCE:
        # Neuer Wert weicht zu stark vom letzten Verdachtswert ab --
        # vermutlich erneutes Rauschen statt eines stabilen neuen
        # Zaehlerstands. Beobachtung neu starten.
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

# --- Fehlende Nadel-/Digit-Box-Erkennung --------------------------------
#
# getIntegerFromPredictions() (readTotalConsumption.py) verkettet einfach
# alle von YOLO in einem Bild erkannten Boxen zu einer Ziffernfolge. Wird
# eine Box in einem Durchlauf nicht erkannt (z.B. eine Nadel schlecht
# beleuchtet/teilweise verdeckt, Confidence unter dem conf-Schwellwert in
# predictNeedlesAndCounterArea/predictDigits), fehlt damit eine ganze
# Stelle -- die verbleibenden Ziffern rutschen um eine Zehnerpotenz weiter
# und der resultierende Wert springt um den Faktor 10, statt nur leicht
# daneben zu liegen. Das laesst sich aus der Ziffernfolge selbst nicht
# erkennen, nur aus der Anzahl der Boxen.
#
# Die Wasseruhr hat eine konstante, aber hier bewusst nicht hart codierte
# Anzahl Nadeln/Digits (haengt vom jeweiligen Zaehlermodell/Kamera-Crop ab)
# -- daher wird die erwartete Anzahl aus der Historie gelernt: der
# haeufigste Wert (Modus) unter den letzten erfolgreichen Erkennungen. Eine
# aktuelle Erkennung mit weniger Boxen als diesem gelernten Modus gilt als
# verdaechtig.
NEEDLE_DIGIT_COUNT_LOOKBACK = 40

# So viele Messungen mit uebereinstimmender Boxenanzahl muessen in der
# Historie vorliegen, bevor der gelernte Modus als verlaesslich gilt --
# verhindert Fehlalarme direkt nach einem frischen Start (wenige/keine
# History) oder wenn die Historie selbst noch uneinheitlich ist.
NEEDLE_DIGIT_COUNT_MIN_HISTORY = 10

# Feature-Flag: auf False gesetzt, ist missingNeedleOrDigitDetector() ein
# reines No-op (gibt immer False zurueck, ohne die History abzufragen oder
# zu benachrichtigen) -- z.B. wenn der Detector in der Praxis zu viele
# Falsch-Positive verursacht. Absichtlich nur deaktiviert statt entfernt,
# damit er sich per Codeaenderung dieser einen Zeile wieder scharf schalten
# laesst, ohne die Logik neu schreiben zu muessen. Seit Aktivierung wird der
# Rohwert, den eine verworfene Messung ergeben haette, zusaetzlich mit
# gespeichert (siehe db.Reading.beforeMissingNeedleCheck/
# afterMissingNeedleCheck sowie readTotalConsumption.py:_gettotalconsumption),
# damit ein Verwerfen retrospektiv nachvollziehbar bleibt statt den Wert
# spurlos verschwinden zu lassen.
MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED = True


def _learnExpectedBoxCount(fieldName: str) -> int | None:
    """
    Bestimmt die erwartete Anzahl erkannter Boxen (Nadeln oder Digits) als
    Modus der letzten NEEDLE_DIGIT_COUNT_LOOKBACK erfolgreichen Readings.

    Parameters:
    fieldName (str): "nNeedlesDetected" oder "nDigitsDetected".

    Returns:
    int|None: gelernte erwartete Anzahl, oder None wenn nicht genug
        History mit diesem Feld vorliegt (z.B. frischer Start, oder DB
        stammt noch von vor Einfuehrung dieser Spalten).
    """
    readings = _readingsSinceLastMeterReplacement(NEEDLE_DIGIT_COUNT_LOOKBACK)
    counts = [getattr(r, fieldName) for r in readings if getattr(r, fieldName) is not None]
    if len(counts) < NEEDLE_DIGIT_COUNT_MIN_HISTORY:
        return None
    values, occurrences = np.unique(counts, return_counts=True)
    return int(values[np.argmax(occurrences)])


def missingNeedleOrDigitDetector(nNeedlesDetected: int, nDigitsDetected: int) -> str | None:
    """
    Vergleicht die in der aktuellen Messung erkannte Anzahl Nadel- bzw.
    Digit-Boxen gegen die aus der Historie gelernte erwartete Anzahl (siehe
    Modul-Kommentar oben). Soll VOR der Berechnung von totalconsumption
    aufgerufen werden (siehe readTotalConsumption.py), damit eine
    verdaechtige Messung komplett verworfen werden kann, statt eine um eine
    Zehnerpotenz verschobene Zahl in die Filter-Pipeline zu geben. Loggt
    selbst (analog meterReplacementDetector), damit der Aufrufer nur das
    Ergebnis auswerten muss.

    Parameters:
    nNeedlesDetected (int): Anzahl der in diesem Bild erkannten Nadel-Boxen.
    nDigitsDetected (int): Anzahl der in diesem Bild erkannten Digit-Boxen.

    Returns:
    str|None: der Verwurfsgrund als fertig formulierter Text (wird von
        readTotalConsumption.py in Reading.discardReason gespeichert, damit
        der Entwicklermodus im Frontend anzeigen kann, WARUM eine Messung
        verworfen wurde -- siehe CardLastPhoto.jsx), oder None wenn die
        Anzahl(en) plausibel sind, noch nicht genug History zum Lernen
        vorliegt, oder der Detector per
        MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED deaktiviert ist.
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
    # Bewusst KEINE addNotification() hier: das Frontend zeigt jede
    # Notification ungefiltert im Glocken-Dropdown oben rechts an (siehe
    # DropdownNotification.jsx), das soll dem Nutzer vorbehalten bleiben,
    # wenn wirklich etwas Wichtiges vorliegt (Zaehlertausch-Verdacht,
    # Leckage) -- nicht fuer jeden intern abgefangenen Erkennungsfehler
    # dieses rein defensiven Filters. Das Log bleibt fuer die Diagnose;
    # der Grund wird zusaetzlich ueber den Rueckgabewert in die DB
    # durchgereicht (siehe Docstring oben).
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
        # Keine History vorhanden (z.B. frisch angelegte/geleerte DB) --
        # nichts zum Vergleichen da, Wert unveraendert durchreichen. Ohne
        # diesen Schutz wirft readings[-1] weiter unten IndexError und
        # reisst den gesamten readTotalConsumption-Lauf vor store_reading()
        # ab (siehe git history/PR-Diskussion).
        return consumption
    readings = sorted(readings, key=lambda x: x.time)
    dy = abs(readings[-1].filtered - consumption)
    dt = (datetime.now() - readings[-1].time).total_seconds() / 60

    if dt == 0:
        return consumption
    # Zwei unabhaengige Kriterien, siehe MAXFLOW_ABSOLUTE_JUMP: die Raten-
    # Pruefung (dy/dt) allein laesst bei grossem dt (mehrere verworfene
    # Zwischenmessungen) auch einen sehr grossen dy noch durch.
    if dy/dt > MAXFLOW or dy > MAXFLOW_ABSOLUTE_JUMP:
        # Bevor geklemmt wird: pruefen, ob sich hier bereits ein anhaltender
        # Trend bestaetigt (siehe MAXFLOW_PERSISTENT_CONFIRM_COUNT oben) --
        # sonst wuerde ein echter Rohrbruch nie sichtbar, weil jede neue
        # (echte, hohe) Messung wieder gegen denselben eingefrorenen alten
        # Wert als "Ausreisser" erkannt wuerde.
        pending = getKeyValueStoreValue(MAXFLOW_PENDING_KEY, default="")
        pendingValues = [float(v) for v in pending.split(",") if v != ""]

        if pendingValues and abs(consumption - pendingValues[-1]) > MAXFLOW_PERSISTENT_TOLERANCE:
            # Weicht zu stark vom letzten Verdachtswert ab -- kein
            # konsistenter Trend, sondern vermutlich erneutes Rauschen.
            # Beobachtung neu starten.
            pendingValues = []

        pendingValues.append(consumption)

        if len(pendingValues) >= MAXFLOW_PERSISTENT_CONFIRM_COUNT:
            # Mehrere aufeinanderfolgende Messungen bestaetigen denselben
            # (steigenden) Trend -- kein Einzelfehler mehr. Wert durchlassen,
            # damit detectLeakage() den tatsaechlichen Anstieg sieht, und
            # den Nutzer direkt informieren (dasselbe Muster wie ein
            # Leck-Alarm; ein anhaltender Fluss in dieser Groessenordnung
            # ist per Definition genau das).
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
        # Bewusst KEINE addNotification() hier -- siehe Kommentar in
        # missingNeedleOrDigitDetector: das Glocken-Dropdown im Frontend
        # soll nur wirklich wichtige Ereignisse zeigen (Zaehlertausch-
        # Verdacht, Leckage), nicht jeden intern abgefangenen
        # Erkennungsfehler dieses Ausreisser-Filters.
        logger.logger.info(f"Outlier detected: {readings[-1].time} {readings[-1].filtered} {consumption} {median}")
        return median

    # Kein Ausreisser -- laufende Verdachtsbeobachtung (falls vorhanden)
    # verwerfen, da die Kette konsistenter Ausreisser unterbrochen ist.
    setKeyValueStoreValue(MAXFLOW_PENDING_KEY, "")
    return consumption

# --- negativeDeltaDetector: Rueckgang vs. persistenter neuer Trend -------
#
# Ein Wasserzaehler kann physikalisch nicht sinken -- JEDER Rueckgang ist
# also ein Fehler irgendwo in der Erkennung. Die urspruengliche Schwelle
# (0.1 m³ = 100 Liter) liess in der Praxis auf dem Pi fast jeden Rueckgang
# unveraendert durch: 22 von 24 beobachteten negativen Spruengen in einer
# realen Messreihe lagen zwischen -0.001 und -0.09 m³, also unterhalb der
# Schwelle (siehe Chat-Analyse) -- ein nachts beobachtetes Oszillieren
# zwischen z.B. 85.4389/85.4489/85.489 waere so nie aufgefallen.
#
# NEGDELTATHRESHOLD daher auf eine reine Rundungstoleranz gesenkt (Floats
# aus round(..., 5) in readTotalConsumption.py) -- aber ein einfaches
# "jeder Rueckgang wird verworfen" waere selbst gefaehrlich: geht eine
# EINZELNE Messung faelschlich zu HOCH aus (z.B. eine YOLO-Fehlklassifikation
# mit +1 an einer Stelle) und pendelt sich der Zaehler danach wieder auf
# seinem echten, niedrigeren Verlauf ein, saehen alle nachfolgenden -- in
# Wahrheit korrekten -- Messungen dauerhaft wie "Rueckgang" gegenueber dem
# falsch-hohen Referenzwert aus und wuerden fuer immer blockiert.
#
# Loesung: derselbe Persistenz-Gedanke wie bei meterReplacementDetector,
# aber verallgemeinert auf jeden Rueckgang (nicht nur den >1.0 m³
# Zaehlertausch-Sonderfall) und diesmal wirkt sie tatsaechlich auf den
# gefilterten Wert (nicht nur Benachrichtigung):
#   - Ein isolierter Rueckgang (naechste Messung liegt wieder nahe am alten
#     Referenzwert) ist ein einmaliger Erkennungsfehler -> verworfen, alter
#     Wert bleibt bestehen.
#   - Ein Rueckgang, der ueber mehrere aufeinanderfolgende Messungen
#     konsistent bleibt, ist kein Zufall mehr -- der ALTE Referenzwert war
#     der Fehler (z.B. der in meterReplacementDetector beschriebene
#     Hoch-Ausreisser), und der neue, niedrigere Trend wird ab Bestaetigung
#     uebernommen.
# Waehrend der Wartephase (Pending-Zaehler noch nicht erreicht) bleibt der
# zurueckgegebene Wert bewusst auf dem alten Stand eingefroren -- bereits
# gespeicherte 'filtered'-Werte aus dieser Wartephase werden nicht
# rueckwirkend korrigiert, nur ab Bestaetigung greift der neue Trend.
NEGDELTATHRESHOLD = 0.0005  # m³, reine Rundungstoleranz

# So viele aufeinanderfolgende rohe Messungen muessen konsistent unterhalb
# des bisherigen Referenzwerts bleiben, bevor der neue, niedrigere Trend
# als echt gilt statt als einmaliger Ausreisser verworfen zu werden.
NEGATIVE_DELTA_CONFIRM_COUNT = 3

# Toleranz, innerhalb derer aufeinanderfolgende rohe Messungen noch als
# "konsistent nahe am neuen, niedrigeren Stand" gelten (deckt normales
# OCR-Rauschen sowie ein wenig echten Verbrauch am neuen Stand ab).
NEGATIVE_DELTA_CONSISTENCY_TOLERANCE = 0.05  # m³

NEGATIVE_DELTA_PENDING_KEY = "negativeDeltaPendingReadings"


def negativeDeltaDetector(consumption: float) -> float:
    readings = _readingsSinceLastMeterReplacement(10)
    if len(readings) == 0:
        # Siehe Kommentar in maxFlowDetector() -- keine History, nichts zu
        # vergleichen.
        return consumption
    readings = sorted(readings, key=lambda x: x.time)
    median = np.median([x.filtered for x in readings])

    if consumption >= (median - NEGDELTATHRESHOLD):
        # Kein Rueckgang (oder innerhalb der Rundungstoleranz) -- laufende
        # Verdachtsbeobachtung (falls vorhanden) verwerfen, da die Kette
        # unterbrochen ist, und den Wert unveraendert durchreichen.
        setKeyValueStoreValue(NEGATIVE_DELTA_PENDING_KEY, "")
        return consumption

    pending = getKeyValueStoreValue(NEGATIVE_DELTA_PENDING_KEY, default="")
    pendingValues = [float(v) for v in pending.split(",") if v != ""]

    if pendingValues and abs(consumption - pendingValues[-1]) > NEGATIVE_DELTA_CONSISTENCY_TOLERANCE:
        # Neuer Wert weicht zu stark vom letzten Verdachtswert ab --
        # vermutlich erneutes Rauschen statt eines konsistenten neuen
        # Stands. Beobachtung neu starten.
        pendingValues = []

    pendingValues.append(consumption)

    if len(pendingValues) >= NEGATIVE_DELTA_CONFIRM_COUNT:
        # Der Rueckgang hat sich ueber mehrere Messungen bestaetigt -- der
        # ALTE Referenzwert war der Fehler, nicht diese Messung. Neuen Trend
        # ab jetzt uebernehmen.
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
