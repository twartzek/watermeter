from peewee import *
from datetime import datetime, timedelta
import os
import shutil
from mylog import Logger

from dotenv import dotenv_values

config = dotenv_values("watermeter/.env")

logger = Logger("db")


# journal_mode=wal + busy_timeout: without these, in the default rollback
# journal mode a connection (e.g. the readTotalConsumption.py measurement
# process run by cron, which could take up to 20 minutes) holds an
# exclusive lock, against which a concurrent write from the FastAPI backend
# (e.g. DELETE /api/v1/readings/{id}) blocks practically indefinitely
# without a timeout -- observed as a "hanging" second delete attempt after
# the first one went through. WAL allows concurrent reads and writes,
# busy_timeout makes a remaining lock conflict raise an error after a short
# wait instead of hanging forever.
db = SqliteDatabase(config["db"], pragmas={"journal_mode": "wal", "busy_timeout": 5000})


class Reading(Model):
    time = DateTimeField(unique=True)
    totalconsumption = FloatField(null=True)
    filtered = FloatField(null=True)
    imageName = CharField()
    # Raw number of needle/digit boxes YOLO detected in this image (see
    # readTotalConsumption.py:identifyNeedlesCorrectionFactor
    # / getIntegerFromPredictions). Needed so that
    # outlierDetection.missingNeedleOrDigitDetector can learn from the
    # history how many boxes are usually detected, and discard readings with
    # too few boxes (e.g. a covered/poorly detected needle) as outliers
    # instead of producing a number shifted by an order of magnitude.
    # null=True, since older readings (from before these columns existed)
    # and totalconsumption=None cases (no detection) have no value for it.
    nNeedlesDetected = IntegerField(null=True)
    nDigitsDetected = IntegerField(null=True)
    # Intermediate value after EACH individual step of the filter pipeline
    # in readTotalConsumption.py (missingDigitDetector -> maxFlowDetector ->
    # negativeDeltaDetector, see there). Without these columns,
    # totalconsumption+filtered alone couldn't tell WHICH of the three
    # filters changed a given value -- e.g. when hunting down an oscillating
    # needle/digit, this had to be re-simulated externally first (see chat
    # analysis of the real Pi measurement series).
    # null=True for the same reasons as nNeedlesDetected/nDigitsDetected.
    afterMissingDigit = FloatField(null=True)
    afterMaxFlow = FloatField(null=True)
    afterNegativeDelta = FloatField(null=True)  # == filtered, but explicitly named as the last step
    # Value before/after outlierDetection.missingNeedleOrDigitDetector (see
    # there and readTotalConsumption.py:_gettotalconsumption). This check
    # runs BEFORE the filter pipeline (missingDigitDetector/maxFlowDetector/
    # negativeDeltaDetector) and is not a value-transforming filter but a
    # discard yes/no decision -- so beforeMissingNeedleCheck holds the raw
    # value as it would be computed WITHOUT regard to this check (always set
    # as long as any boxes were detected), afterMissingNeedleCheck is either
    # the same value (unremarkable) or None (discarded). That way the DB
    # shows which (potentially order-of-magnitude-shifted) value a discarded
    # reading would have produced, instead of it being lost without a trace.
    beforeMissingNeedleCheck = FloatField(null=True)
    afterMissingNeedleCheck = FloatField(null=True)
    # The reason text returned by outlierDetection.missingNeedleOrDigitDetector
    # if this reading was discarded because of it (None otherwise). Lets
    # developer mode in the frontend (CardLastPhoto.jsx) show WHY a reading
    # with totalconsumption=None was discarded, instead of just "no
    # detection" without further explanation. TextField instead of
    # CharField, since the reason text (several clauses when needles AND
    # digits are affected) can exceed CharField's default length of 255.
    discardReason = TextField(null=True)
    # beforeRolloverCorrection/afterRolloverCorrection: the rollover
    # ambiguity correction that filled these values
    # (outlierDetection.resolveRolloverAmbiguity/resolveDigitNeedleCarry)
    # repeatedly corrupted real, correct readings (e.g. 89.9774 -> 88.2482 on
    # Sep 24, after an earlier incident 89.x -> 80.x) and was therefore
    # removed entirely instead of being patched further. The columns stay
    # nullable in the DB (old readings keep their historical values) but are
    # no longer filled for new readings.
    beforeRolloverCorrection = FloatField(null=True)
    afterRolloverCorrection = FloatField(null=True)

    class Meta:
        database = db

class Notification(Model):
    time = DateTimeField(unique=True)
    message = CharField()
    i18nIdentifier = CharField()
    type = CharField()
    viewed = BooleanField(default=False)

    class Meta:
        database = db

class KeyValueStore(Model):
    key = CharField(unique=True)
    value = CharField()

    class Meta:
        database = db

class MeterReplacement(Model):
    """
    Records a user-confirmed physical water meter replacement (see
    outlierDetection.confirmMeterReplacement). `time` marks the point after
    which readings belong to the new meter; consumption aggregations
    (getConPerYear/Month/Day) use this to avoid summing across the
    reset as if it were negative consumption.
    """
    time = DateTimeField(unique=True)

    class Meta:
        database = db


def create_db_if_not_exists():
    if not os.path.isfile(config["db"]):
        init_db()
        return
    if os.path.getsize(config["db"]) == 0:
        init_db()
        return
    # DB file already exists (e.g. from before the MeterReplacement table
    # existed) -- create_tables(safe=True) only creates missing tables and
    # leaves existing ones alone, so it is safe to call on every start.
    init_db()


def init_db():
    logger.logger.info("Creating database")
    db.connect(reuse_if_open=True)
    db.create_tables([Reading, Notification, KeyValueStore, MeterReplacement], safe=True)
    _migrate_add_missing_columns()
    db.close()


def _migrate_add_missing_columns():
    """
    create_tables(safe=True) only creates missing TABLES -- a new column on
    an already existing table (like nNeedlesDetected/nDigitsDetected/
    afterMissingDigit/... on Reading) is not touched by it. Without this
    migration, every access to the new fields on an older DB file would
    fail with "no such column". Idempotent -- checks for each column
    whether it already exists before adding it via ALTER TABLE, so it is
    safe to call on every start. The SQL type is derived from the peewee
    field type (IntegerField -> INTEGER, FloatField -> REAL, ...) instead of
    assuming INTEGER -- SQLite's type affinity would store a float losslessly
    in an INTEGER column too, but the schema should reflect the actual
    field type.
    """
    existing_columns = {col.name for col in db.get_columns(Reading._meta.table_name)}
    for field_name, field in Reading._meta.fields.items():
        column_name = field.column_name
        if column_name in existing_columns:
            continue
        sql_type = field.field_type  # peewee: "INT", "FLOAT", "VARCHAR", ...
        db.execute_sql(
            f'ALTER TABLE "{Reading._meta.table_name}" ADD COLUMN "{column_name}" {sql_type}'
        )
        logger.logger.info(f"Migrated DB: added column {column_name} ({sql_type}) to Reading")

def addNotification(message,i18nIdentifier, type):
    Notification.create(time=datetime.now(), message=message, i18nIdentifier=i18nIdentifier, type=type)

def getAllNotifications(youngerThan: datetime=None):
    if youngerThan is not None:
        query = Notification.select().where(Notification.time > youngerThan).order_by(Notification.time.desc())
        return query.dicts()
    query = Notification.select().order_by(Notification.time.desc())
    return [item for item in query.dicts()]

def updateViewedNotifications(notifications: list):
    for notification in notifications:
        item = Notification.get(Notification.id == notification.id)
        item.viewed = True
        item.save()

def deleteAllNotifications():
    Notification.delete().execute()


def getKeyValueStoreValue(key, default=None):
    try:
        item = KeyValueStore.get(KeyValueStore.key == key)
        return item.value
    except KeyValueStore.DoesNotExist:
        return default

def setKeyValueStoreValue(key, value):
    try:
        item = KeyValueStore.get(KeyValueStore.key == key)
        item.value = value
        item.save()
    except KeyValueStore.DoesNotExist:
        item = KeyValueStore.create(key=key, value=value)


def addMeterReplacement(time: datetime):
    return MeterReplacement.create(time=time)


def getMeterReplacements():
    """All confirmed meter replacement timestamps, oldest first."""
    return [r.time for r in MeterReplacement.select().order_by(MeterReplacement.time.asc())]


def getLastMeterReplacement():
    """Most recently confirmed meter replacement timestamp, or None."""
    row = MeterReplacement.select().order_by(MeterReplacement.time.desc()).first()
    return row.time if row else None


def getCumulativeTotal():
    """
    After a replacement, the current reading of the most recently installed
    meter is no longer the overall total -- it starts again at ~0. This
    function instead returns the total carried forward across all
    confirmed replacements: the last raw value plus, for each replacement,
    the absolute final reading of the meter installed before it (not just
    the jump height -- unlike the consumption correction in
    _correctConsumptionForMeterReplacements, which works on deltas, this is
    an absolute value).

    Without a replacement the result is simply the last meter reading.

    Returns:
        float|None: corrected total, or None without readings.
    """
    last = (
        Reading.select()
        .where(Reading.totalconsumption.is_null(False))
        .order_by(Reading.time.desc())
        .first()
    )
    if last is None:
        return None

    cumulative = last.filtered
    for replacement in MeterReplacement.select().order_by(MeterReplacement.time.asc()):
        before = (
            Reading.select()
            .where((Reading.time < replacement.time) & Reading.totalconsumption.is_null(False))
            .order_by(Reading.time.desc())
            .first()
        )
        after = (
            Reading.select()
            .where((Reading.time >= replacement.time) & Reading.totalconsumption.is_null(False))
            .order_by(Reading.time.asc())
            .first()
        )
        if before is None or after is None:
            continue
        if before.filtered > after.filtered:
            cumulative += before.filtered
    return cumulative


def _correctConsumptionForMeterReplacements(start_time: datetime, end_time: datetime, raw_consumption: float) -> float:
    """
    Adjust a naive last-minus-first consumption figure for a time period
    that may contain one or more confirmed meter replacements.

    Without this, a period spanning a replacement would show a large
    negative "consumption" -- the reading dropped from e.g. 435 m^3 back to
    ~0 m^3 when the physical meter was swapped, which last-first would
    read as -435 m^3 of consumption. Each replacement introduces exactly
    one such artificial downward jump (old meter's last reading -> new
    meter's first reading afterwards) into that raw figure, so adding back
    the size of each jump found strictly between start and end cancels it
    out again, leaving the sum of genuine consumption on both sides of the
    swap.

    Args:
        start_time: time of the period's first reading (already known not
            to be None -- callers only invoke this once they have a
            first/last pair).
        end_time: time of the period's last reading.
        raw_consumption: end.filtered - start.filtered for the period.

    Returns:
        float: consumption corrected for any replacements in (start, end).
    """
    replacements = (
        MeterReplacement.select()
        .where((MeterReplacement.time > start_time) & (MeterReplacement.time <= end_time))
        .order_by(MeterReplacement.time.asc())
    )
    corrected = raw_consumption
    for replacement in replacements:
        before = (
            Reading.select()
            .where((Reading.time < replacement.time) & Reading.totalconsumption.is_null(False))
            .order_by(Reading.time.desc())
            .first()
        )
        after = (
            Reading.select()
            .where((Reading.time >= replacement.time) & Reading.totalconsumption.is_null(False))
            .order_by(Reading.time.asc())
            .first()
        )
        if before is None or after is None:
            continue
        jump = before.filtered - after.filtered
        if jump > 0:
            corrected += jump
    return corrected


def store_reading(
    totalconsumption, filtered, image_source_file_path,
    nNeedlesDetected=None, nDigitsDetected=None,
    afterMissingDigit=None, afterMaxFlow=None, afterNegativeDelta=None,
    beforeMissingNeedleCheck=None, afterMissingNeedleCheck=None,
    discardReason=None,
):
    logger.logger.info("Started store_reading")
    imageName = os.path.basename(image_source_file_path)
    logger.logger.info("Storing image to database")
    reading = Reading.create(
        time=datetime.now(),
        totalconsumption=totalconsumption,
        filtered=filtered,
        imageName=imageName,
        nNeedlesDetected=nNeedlesDetected,
        nDigitsDetected=nDigitsDetected,
        afterMissingDigit=afterMissingDigit,
        afterMaxFlow=afterMaxFlow,
        afterNegativeDelta=afterNegativeDelta,
        beforeMissingNeedleCheck=beforeMissingNeedleCheck,
        afterMissingNeedleCheck=afterMissingNeedleCheck,
        discardReason=discardReason,
    )
    logger.logger.info("Stored image to database. ID: " + str(reading.id))


def get_newest_image():
    """
    Scan the images directory directly and return (filename, taken_at) for
    the most recently created photo, or None if the directory is empty.

    This is a fallback for the developer mode's "show the last taken
    photo" view: readTotalConsumption.py always saves the photo to disk
    via getImage() before running detection, but only calls store_reading()
    once detection has produced a value or explicitly failed with
    totalconsumption=None -- if detection raises an exception instead (a
    bug, a corrupt frame, ...), store_reading() is never reached and the
    photo has no DB row at all. Scanning the directory means the developer
    mode can still show that photo instead of silently falling behind.

    Bounding-box overlay files (imageName + "_bbox.jpg", written by
    gettotalconsumption() next to the original photo) are excluded so a
    freshly written overlay for an older photo is never mistaken for a
    newer capture.
    """
    try:
        entries = [
            f for f in os.listdir(config["images"]) if not f.endswith("_bbox.jpg")
        ]
    except FileNotFoundError:
        return None
    if not entries:
        return None
    filename = max(
        entries, key=lambda f: os.path.getmtime(os.path.join(config["images"], f))
    )
    taken_at = datetime.fromtimestamp(
        os.path.getmtime(os.path.join(config["images"], filename))
    )
    return filename, taken_at


def get_last_readings(x, only_successful=True):
    """
    Get the last X readings from the database.

    Args:
        x (int): Number of readings to retrieve.
        only_successful (bool): If True (default), only readings with a
            successfully detected totalconsumption are returned. Pass False
            to also include readings where the detection failed (used by
            the developer mode to still show the last taken photo).

    Returns:
        list: List of Reading objects representing the last X readings.
    """
    query = Reading.select()
    if only_successful:
        query = query.where(Reading.totalconsumption.is_null(False))
    readings = query.order_by(Reading.time.desc()).limit(x)
    return readings


def get_all_readings(mode:str="filtered"):
    """
    Get the all readings from the database.

    Returns:
        list: List of Reading objects representing the last X readings.
    """
    readings = Reading.select().order_by(Reading.time.desc())
    if mode == "filtered":
        return [
            {"datetime": x.time, "totalconsumption": x.filtered} for x in readings
        ]
    else:
        return [{"datetime": x.time, "totalconsumption": x.totalconsumption} for x in readings]


def get_readings_since(days: int):
    """
    Get all readings from the last `days` days, oldest first.

    Args:
        days (int): Number of days to look back from now.

    Returns:
        list: List of Reading objects, ordered by time ascending.
    """
    since = datetime.now() - timedelta(days=days)
    return list(Reading.select().where(Reading.time >= since).order_by(Reading.time.asc()))


def get_readings_between(start: datetime, end: datetime, only_successful=True):
    """
    Reads all data elements from a database table that fall between two given datetime values.

    Args:
        start (datetime): Start datetime.
        end (datetime): End datetime.
        only_successful (bool): If True (default), only readings with a
            successfully detected totalconsumption are returned. Pass False
            to also include readings where the detection failed (developer
            mode).

    Returns:
        list: List of rows that match the datetime range.
    """
    # Query the database
    query = Reading.select().where(Reading.time.between(start, end))
    if only_successful:
        query = query.where(Reading.totalconsumption.is_null(False))
    query = query.order_by(Reading.time.desc())
    # rows = query.execute()

    # Convert rows to a list of dictionaries
    # data = [{"datetime": row.time, "totalconsumption": row.totalconsumption, "imageName": row.imageName} for row in query]

    return query


def getConsumptionBetween(start: datetime, end: datetime):
    """
    Consumption between two arbitrary datetimes (e.g. "this week"), as the
    last reading in the range minus the first, corrected for any confirmed
    meter replacements in between (see _correctConsumptionForMeterReplacements).

    Unlike getConPerYear/Month/Day, which group by calendar period, this
    takes an explicit range -- used for periods (like a Luxon-computed
    week) that don't map onto a single strftime group.

    Returns:
        dict: {"first": float|None, "last": float|None, "consumption": float|None}
        with all three None if there are no readings in range.
    """
    query = (
        Reading.select()
        .where(Reading.time.between(start, end))
        .where(Reading.totalconsumption.is_null(False))
        .order_by(Reading.time.asc())
    )
    readings = list(query)
    if not readings:
        return {"first": None, "last": None, "consumption": None}

    first = readings[0]
    last = readings[-1]
    rawConsumption = last.filtered - first.filtered
    consumption = _correctConsumptionForMeterReplacements(first.time, last.time, rawConsumption)

    return {"first": first.filtered, "last": last.filtered, "consumption": consumption}


# How many days of photos to keep for every reading before thinning a day
# down to the photos of its first/last reading. The readings themselves are
# never thinned out: at ~100 bytes per row they cost ~4 MB per year, while
# the photos are what actually fills the SD card. Keeping every reading
# also lets the leakage detector's adaptive models (see leakageDetector.py)
# use a longer baseline than this if BASELINE_LOOKBACK_DAYS is raised.
FULL_RESOLUTION_IMAGE_RETENTION_DAYS = 30


def thin_out_old_images():
    """
    For every day older than FULL_RESOLUTION_IMAGE_RETENTION_DAYS, deletes
    all photos (incl. _bbox.jpg) except those of the day's first and last
    reading. The reading rows are kept completely -- their imageName may
    then point to a file that no longer exists (restapi.py returns
    imageUrl=None for those).
    """
    # Round down to midnight so a day in progress is never thinned out (its
    # 'last' reading wouldn't actually be the last one yet).
    cutoff = (datetime.now() - timedelta(days=FULL_RESOLUTION_IMAGE_RETENTION_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # Per day, keep the image names of the first and last reading and mark
    # all other image names of that day for deletion.
    keep = set()
    candidates = set()
    currentDay = None
    dayNames = []

    def _closeDay():
        if dayNames:
            keep.add(dayNames[0])
            keep.add(dayNames[-1])
            candidates.update(dayNames[1:-1])

    rows = (
        Reading.select(Reading.time, Reading.imageName)
        .where(Reading.time < cutoff)
        .order_by(Reading.time.asc())
        .tuples()
    )
    for time, imageName in rows:
        day = time.date()
        if day != currentDay:
            _closeDay()
            currentDay = day
            dayNames = []
        dayNames.append(imageName)
    _closeDay()

    # Iterate over the directory instead of the (over time very many) old
    # readings: from the second run on only a few files per old day remain
    # there, so the nightly job stays fast even after years.
    toDelete = candidates - keep
    try:
        entries = os.listdir(config["images"])
    except FileNotFoundError:
        entries = []
    for filename in entries:
        if filename not in toDelete:
            continue
        for name in (filename, filename + "_bbox.jpg"):
            path = os.path.join(config["images"], name)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                logger.logger.warning(f"Failed to remove old image {path}")

    delete_orphaned_images(cutoff)


def delete_orphaned_images(cutoff: datetime):
    """
    Deletes image files in config["images"] that are older than 'cutoff'
    and have no (longer a) matching reading entry.

    thin_out_old_images() only handles images referenced by a reading --
    photos that never got a DB row, because detection in
    readTotalConsumption.py aborted with an exception before
    store_reading() was reached (see the get_newest_image() docstring),
    would otherwise stay on the SD card forever. In practice they make up
    most of the storage used by the image folder (far more orphaned photos
    than actual reading rows).

    'cutoff' is deliberately identical to thin_out_old_images(), so the last
    FULL_RESOLUTION_IMAGE_RETENTION_DAYS days stay untouched -- even an
    orphaned image from 5 minutes ago should still be visible via the
    developer mode fallback (get_newest_image()).
    """
    try:
        entries = os.listdir(config["images"])
    except FileNotFoundError:
        return

    known_image_names = {r.imageName for r in Reading.select(Reading.imageName)}

    for filename in entries:
        if filename.endswith("_bbox.jpg"):
            # Belongs to the original without the suffix; only deleted
            # together with the original if that is orphaned (see below).
            continue
        if filename in known_image_names:
            continue
        filepath = os.path.join(config["images"], filename)
        try:
            if datetime.fromtimestamp(os.path.getmtime(filepath)) >= cutoff:
                continue
            os.remove(filepath)
            bbox_path = os.path.join(config["images"], filename + "_bbox.jpg")
            if os.path.exists(bbox_path):
                os.remove(bbox_path)
        except OSError:
            logger.logger.warning(f"Failed to remove orphaned image {filepath}")

def fill_database_with_dummy_data():
    """
    Fill the database with dummy data for the last 24 months.
    """
    # Define the start date
    start_date = datetime.now() - timedelta(days=312)  # 13 months ago

    # Define the number of readings per month
    num_readings_per_month = 5

    dummytotalconsumption = 0
    # Loop through the months
    for month in range(13):
        # Calculate the date for this month
        month_date = start_date + timedelta(days=month * 30)

        # Loop through the readings for this month
        for reading in range(num_readings_per_month):
            # Calculate the date for this reading
            reading_date = month_date + timedelta(days=reading * 3)

            imgname = f"{reading_date.year}-{reading_date.month}-{reading_date.day}.jpg"
            shutil.copyfile(
                "Reading/backend/noReading.jpg",
                os.path.join(config["images"], imgname),
            )
            # Create a new Reading object
            reading = Reading(
                time=reading_date,
                totalconsumption=dummytotalconsumption,  # dummy value
                filtered=dummytotalconsumption,
                imageName=imgname,
            )

            # Save the Reading object to the database
            reading.save()
            dummytotalconsumption += 10











def _firstOrLastSuccessfulReading(start: datetime | None = None, end: datetime | None = None, last: bool = False):
    """
    First or last successful reading in the interval [start, end). Uses the
    index on Reading.time (range filter + ORDER BY time LIMIT 1) instead of
    reading the whole table like strftime() filters/groupings do.
    """
    query = Reading.select().where(Reading.totalconsumption.is_null(False))
    if start is not None:
        query = query.where(Reading.time >= start)
    if end is not None:
        query = query.where(Reading.time < end)
    return query.order_by(Reading.time.desc() if last else Reading.time.asc()).first()


def getConPerYear():
    # Since readings are no longer thinned out, the table grows by ~35,000
    # rows per year. Instead of partitioning all rows by year with a window
    # function, two index lookups per year (first and last reading) are
    # enough.
    overallFirst = _firstOrLastSuccessfulReading()
    overallLast = _firstOrLastSuccessfulReading(last=True)
    if overallFirst is None or overallLast is None:
        return []

    results = []
    for y in range(overallFirst.time.year, overallLast.time.year + 1):
        yearStart = datetime(y, 1, 1)
        yearEnd = datetime(y + 1, 1, 1)
        first = _firstOrLastSuccessfulReading(yearStart, yearEnd)
        if first is None:
            continue
        last = _firstOrLastSuccessfulReading(yearStart, yearEnd, last=True)
        rawConsumption = last.filtered - first.filtered
        results.append(
            {
                "year": str(y),
                "first": first.filtered,
                "last": last.filtered,
                "consumption": _correctConsumptionForMeterReplacements(
                    first.time, last.time, rawConsumption
                ),
                "rate": None,
            }
        )

    for index, item in enumerate(results):
        if index>0:
            if results[index-1]["consumption"]!=0:
                results[index]["rate"]=100*(results[index]["consumption"]-results[index-1]["consumption"])/(results[index-1]["consumption"])

    return results


def getConPerMonth(year: int):

    month_year = fn.strftime("%Y-%m", Reading.time).alias("month_year")


    # Build queries that return the first and last reading per period.
    # The window function is defined directly in a subquery.
    # Important: NULL values are already filtered out in this inner select.

    # The subquery that ranks the non-NULL readings and extracts the
    # period.
    subQueryFirst = (
        Reading.select(
            month_year,
            Reading.totalconsumption,
            Reading.filtered,
            Reading.time,
            fn.ROW_NUMBER()
            .over(
                partition_by=[fn.STRFTIME("%Y-%m", Reading.time)],
                order_by=Reading.time.asc(),
            )
            .alias("rn"),
        )
        .where((Reading.time >= datetime(year, 1, 1)) & (Reading.time < datetime(year + 1, 1, 1)))
        .where(Reading.totalconsumption.is_null(False))  # filter out NULL values here
    )

    subQueryLast = (
        Reading.select(
            month_year,
            Reading.totalconsumption,
            Reading.filtered,
            Reading.time,
            fn.ROW_NUMBER()
            .over(
                partition_by=[fn.STRFTIME("%Y-%m", Reading.time)],
                order_by=Reading.time.desc(),
            )
            .alias("rn"),
        )
        .where((Reading.time >= datetime(year, 1, 1)) & (Reading.time < datetime(year + 1, 1, 1)))
        .where(Reading.totalconsumption.is_null(False))  # filter out NULL values here
    )


    # The main query, which selects and filters from the subquery
    queryFirst = (
        Reading.select(
            subQueryFirst.c.month_year,
            subQueryFirst.c.totalconsumption,
            subQueryFirst.c.filtered,
            subQueryFirst.c.time,
        )
        .from_(subQueryFirst)  # select from the named subquery
        .where(subQueryFirst.c.rn == 1)  # keep only the first rank
        .order_by(subQueryFirst.c.month_year)
    )


    queryLast = (
        Reading.select(
            subQueryLast.c.month_year,
            subQueryLast.c.totalconsumption,
            subQueryLast.c.filtered,
            subQueryLast.c.time,
        )
        .from_(subQueryLast)  # select from the named subquery
        .where(subQueryLast.c.rn == 1)  # keep only the first rank
        .order_by(subQueryLast.c.month_year)
    )


    resultsFirst = list(queryFirst.dicts())
    resultsLast = list(queryLast.dicts())



    results = []

    for index, item in enumerate(resultsLast):
        rawConsumption = item["filtered"] - resultsFirst[index]["filtered"]
        results.append(
            {
                "year-month": item["month_year"],
                "first": resultsFirst[index]["filtered"],
                "last": item["filtered"],
                "consumption": _correctConsumptionForMeterReplacements(
                    resultsFirst[index]["time"], item["time"], rawConsumption
                ),
                "rate": None,
            }
        )

    for index, item in enumerate(results):
        if index>0:
            if results[index-1]["consumption"]!=0:
                results[index]["rate"]=100*(results[index]["consumption"]-results[index-1]["consumption"])/(results[index-1]["consumption"])



    resultsFilled = []
    for i in range(1,13):
        conExist = False
        for r in results:
            if int(r["year-month"].split("-")[1])==i:
                resultsFilled.append(r)
                conExist = True
                break
        if not conExist:
            resultsFilled.append({"year-month":f"{year}-{i:02d}", "first":None, "last":None, "consumption":None, "rate":None})



    return resultsFilled

def _startOfNextMonth(year: int, month: int) -> datetime:
    return datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)


def getConPerDay(year: int, month: int):

    day = fn.strftime("%Y-%m-%d", Reading.time).alias("day")


    # Build queries that return the first and last reading per period.
    # The window function is defined directly in a subquery.
    # Important: NULL values are already filtered out in this inner select.

    # The subquery that ranks the non-NULL readings and extracts the
    # period.
    subQueryFirst = (
        Reading.select(
            day,
            Reading.totalconsumption,
            Reading.filtered,
            Reading.time,
            fn.ROW_NUMBER()
            .over(
                partition_by=[fn.STRFTIME("%Y-%m-%d", Reading.time)],
                order_by=Reading.time.asc(),
            )
            .alias("rn"),
        )
        .where((Reading.time >= datetime(year, month, 1)) & (Reading.time < _startOfNextMonth(year, month)))
        .where(Reading.totalconsumption.is_null(False))  # filter out NULL values here
    )

    subQueryLast = (
        Reading.select(
            day,
            Reading.totalconsumption,
            Reading.filtered,
            Reading.time,
            fn.ROW_NUMBER()
            .over(
                partition_by=[fn.STRFTIME("%Y-%m-%d", Reading.time)],
                order_by=Reading.time.desc(),
            )
            .alias("rn"),
        )
        .where((Reading.time >= datetime(year, month, 1)) & (Reading.time < _startOfNextMonth(year, month)))
        .where(Reading.totalconsumption.is_null(False))  # filter out NULL values here
    )


    # The main query, which selects and filters from the subquery
    queryFirst = (
        Reading.select(
            subQueryFirst.c.day,
            subQueryFirst.c.totalconsumption,
            subQueryFirst.c.filtered,
            subQueryFirst.c.time,
        )
        .from_(subQueryFirst)  # select from the named subquery
        .where(subQueryFirst.c.rn == 1)  # keep only the first rank
        .order_by(subQueryFirst.c.day)
    )


    queryLast = (
        Reading.select(
            subQueryLast.c.day,
            subQueryLast.c.totalconsumption,
            subQueryLast.c.filtered,
            subQueryLast.c.time,
        )
        .from_(subQueryLast)  # select from the named subquery
        .where(subQueryLast.c.rn == 1)  # keep only the first rank
        .order_by(subQueryLast.c.day)
    )


    resultsFirst = list(queryFirst.dicts())
    resultsLast = list(queryLast.dicts())



    results = []



    for index, item in enumerate(resultsLast):
        rawConsumption = item["filtered"] - resultsFirst[index]["filtered"]
        results.append(
            {
                "day": int(item["day"].split("-")[2]),
                "first": resultsFirst[index]["filtered"],
                "last": item["filtered"],
                "consumption": _correctConsumptionForMeterReplacements(
                    resultsFirst[index]["time"], item["time"], rawConsumption
                ),
                "rate": None,
            }
        )



    for index, item in enumerate(results):
        if index>0:
            if results[index-1]["consumption"]!=0:
                results[index]["rate"]=100*(results[index]["consumption"]-results[index-1]["consumption"])/(results[index-1]["consumption"])



    resultsFilled = []
    for i in range(1,32):
        conExist = False
        for r in results:
            if r["day"]==i:
                resultsFilled.append(r)
                conExist = True
                break
        if not conExist:
            resultsFilled.append({"day":i, "first":None, "last":None, "consumption":None, "rate":None})



    return resultsFilled


def deleteReading(id:int):
    reading = Reading.get(Reading.id == id)
    if reading:
        filename = reading.imageName
        if os.path.exists(os.path.join(config["images"], filename)):
            os.remove(os.path.join(config["images"], filename))
        if os.path.exists(os.path.join(config["images"], filename+"_bbox.jpg")):
            os.remove(os.path.join(config["images"], filename+"_bbox.jpg"))
        return reading.delete_instance()
    return None


if __name__ == "__main__":
    # create_db_if_not_exists()
    # init_db()
    # fill_database_with_dummy_data()
    # thin_out_old_images()

    # print(getAllNotifications())
    thin_out_old_images()

    pass


