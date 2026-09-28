"""
Tests for the developer-mode support in db.py:

- store_reading() must accept totalconsumption=None (a failed detection)
  and still create a Reading row, so the photo stays discoverable via the
  API instead of vanishing silently.
- get_last_readings()/get_readings_between() must default to hiding those
  NULL readings (only_successful=True), but include them when the caller
  passes only_successful=False (developer mode).
- get_newest_image() scans the images directory directly, as a fallback
  for photos that never got a DB row at all (detection raised an
  exception before store_reading() was reached).
"""
import os
import time
from datetime import datetime, timedelta


def _touch(path, mtime_epoch):
    """Create an empty file and pin its mtime, so ordering doesn't depend
    on filesystem mtime resolution or real wall-clock timing between
    writes."""
    with open(path, "w"):
        pass
    os.utime(path, (mtime_epoch, mtime_epoch))


def test_store_reading_accepts_failed_detection(db_module):
    db_module.store_reading(None, None, "/images/2026-01-01_00-00-00.jpg")

    readings = list(db_module.Reading.select())
    assert len(readings) == 1
    assert readings[0].totalconsumption is None
    assert readings[0].filtered is None
    assert readings[0].imageName == "2026-01-01_00-00-00.jpg"


def test_store_reading_records_needle_digit_counts_and_filter_trace(db_module):
    # nNeedlesDetected/nDigitsDetected feed missingNeedleOrDigitDetector's
    # learned-history check; afterMissingDigit/afterMaxFlow/afterNegativeDelta
    # record what each individual filter step in readTotalConsumption.py's
    # pipeline did, so it's possible to see afterwards which filter (if any)
    # changed a given value without re-simulating the whole chain.
    db_module.store_reading(
        85.0, 84.9, "ok.jpg",
        nNeedlesDetected=4, nDigitsDetected=5,
        afterMissingDigit=85.0, afterMaxFlow=85.0, afterNegativeDelta=84.9,
    )

    reading = db_module.Reading.select().first()
    assert reading.nNeedlesDetected == 4
    assert reading.nDigitsDetected == 5
    assert reading.afterMissingDigit == 85.0
    assert reading.afterMaxFlow == 85.0
    assert reading.afterNegativeDelta == 84.9


def test_store_reading_records_before_and_after_missing_needle_check_when_accepted(db_module):
    # missingNeedleOrDigitDetector runs BEFORE the filter pipeline and is a
    # discard-yes/no decision, not a value-transforming filter -- when it
    # does NOT flag the reading, before/after carry the same raw value.
    db_module.store_reading(
        85.0, 84.9, "ok.jpg",
        beforeMissingNeedleCheck=85.0, afterMissingNeedleCheck=85.0,
    )

    reading = db_module.Reading.select().first()
    assert reading.beforeMissingNeedleCheck == 85.0
    assert reading.afterMissingNeedleCheck == 85.0


def test_store_reading_records_before_value_even_when_discarded(db_module):
    # When missingNeedleOrDigitDetector DOES flag the reading,
    # totalconsumption/filtered become None (see readTotalConsumption.py's
    # __main__), but beforeMissingNeedleCheck still records what the raw
    # value would have been -- so a discard stays retrospectively visible
    # in the DB instead of vanishing without a trace.
    db_module.store_reading(
        None, None, "discarded.jpg",
        nNeedlesDetected=3, nDigitsDetected=5,
        beforeMissingNeedleCheck=85.9732, afterMissingNeedleCheck=None,
    )

    reading = db_module.Reading.select().first()
    assert reading.totalconsumption is None
    assert reading.filtered is None
    assert reading.beforeMissingNeedleCheck == 85.9732
    assert reading.afterMissingNeedleCheck is None


def test_store_reading_defaults_needle_digit_and_filter_trace_fields_to_none(db_module):
    # Existing callers (and the totalconsumption=None failure path) don't
    # pass these -- must not become a required argument.
    db_module.store_reading(None, None, "failed.jpg")

    reading = db_module.Reading.select().first()
    assert reading.nNeedlesDetected is None
    assert reading.nDigitsDetected is None
    assert reading.afterMissingDigit is None
    assert reading.afterMaxFlow is None
    assert reading.afterNegativeDelta is None
    assert reading.beforeMissingNeedleCheck is None
    assert reading.afterMissingNeedleCheck is None


def test_get_last_readings_hides_failed_detection_by_default(db_module):
    db_module.store_reading(None, None, "failed.jpg")
    db_module.store_reading(12.3, 12.3, "ok.jpg")

    readings = list(db_module.get_last_readings(10))

    assert len(readings) == 1
    assert readings[0].imageName == "ok.jpg"


def test_get_last_readings_includes_failed_detection_in_developer_mode(db_module):
    db_module.store_reading(None, None, "failed.jpg")
    db_module.store_reading(12.3, 12.3, "ok.jpg")

    readings = list(db_module.get_last_readings(10, only_successful=False))

    assert len(readings) == 2
    assert {r.imageName for r in readings} == {"failed.jpg", "ok.jpg"}


def test_get_last_readings_respects_limit_and_order(db_module):
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(3):
        reading = db_module.Reading(
            time=base + timedelta(minutes=i),
            totalconsumption=float(i),
            filtered=float(i),
            imageName=f"img{i}.jpg",
        )
        reading.save()

    readings = list(db_module.get_last_readings(1))

    assert len(readings) == 1
    assert readings[0].imageName == "img2.jpg"  # most recent


def test_get_readings_between_hides_failed_detection_by_default(db_module):
    now = datetime.now()
    ok = db_module.Reading(
        time=now, totalconsumption=1.0, filtered=1.0, imageName="ok.jpg"
    )
    ok.save()
    failed = db_module.Reading(
        time=now + timedelta(seconds=1),
        totalconsumption=None,
        filtered=None,
        imageName="failed.jpg",
    )
    failed.save()

    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    readings = list(db_module.get_readings_between(start, end))

    assert [r.imageName for r in readings] == ["ok.jpg"]


def test_get_readings_between_includes_failed_detection_in_developer_mode(db_module):
    now = datetime.now()
    ok = db_module.Reading(
        time=now, totalconsumption=1.0, filtered=1.0, imageName="ok.jpg"
    )
    ok.save()
    failed = db_module.Reading(
        time=now + timedelta(seconds=1),
        totalconsumption=None,
        filtered=None,
        imageName="failed.jpg",
    )
    failed.save()

    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    readings = list(db_module.get_readings_between(start, end, only_successful=False))

    assert {r.imageName for r in readings} == {"ok.jpg", "failed.jpg"}


def test_get_readings_between_excludes_readings_outside_the_window(db_module):
    now = datetime.now()
    inside = db_module.Reading(
        time=now, totalconsumption=1.0, filtered=1.0, imageName="inside.jpg"
    )
    inside.save()
    outside = db_module.Reading(
        time=now - timedelta(days=10),
        totalconsumption=2.0,
        filtered=2.0,
        imageName="outside.jpg",
    )
    outside.save()

    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    readings = list(db_module.get_readings_between(start, end, only_successful=False))

    assert [r.imageName for r in readings] == ["inside.jpg"]


def test_get_newest_image_returns_none_for_empty_directory(db_module, images_dir):
    assert db_module.get_newest_image() is None


def test_get_newest_image_returns_the_most_recently_modified_file(db_module, images_dir):
    base = time.time()
    _touch(os.path.join(images_dir, "older.jpg"), base - 20)
    _touch(os.path.join(images_dir, "newer.jpg"), base - 10)

    result = db_module.get_newest_image()

    assert result is not None
    filename, taken_at = result
    assert filename == "newer.jpg"
    assert taken_at == datetime.fromtimestamp(base - 10)


def test_get_newest_image_ignores_bbox_overlay_files(db_module, images_dir):
    base = time.time()
    _touch(os.path.join(images_dir, "photo.jpg"), base - 20)
    # The overlay is written *after* the original photo but must never be
    # mistaken for a newer capture -- it's the same reading's annotated
    # copy, not a new one.
    _touch(os.path.join(images_dir, "photo.jpg_bbox.jpg"), base - 5)

    filename, _ = db_module.get_newest_image()

    assert filename == "photo.jpg"
