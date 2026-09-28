"""
Tests for the developer-mode filtering wired up in restapi.py's reading
endpoints. These call the route functions directly (they take no
Depends/Request, so this runs the same code the HTTP layer would run)
against an isolated DB + settings.json, per the restapi_module fixture in
conftest.py.
"""
import os
import time
from datetime import datetime, timedelta

import pytest


def _touch(path, mtime_epoch):
    with open(path, "w"):
        pass
    os.utime(path, (mtime_epoch, mtime_epoch))


def _make_settings(restapi, developer_mode: bool):
    return restapi.Settings(
        mqtt=restapi.Mqtt(broker="b", port=1, username="u", password="p"),
        smtp=restapi.Smtp(
            server="s", port=1, sender="a@b.de", recipient="a@b.de", password="p"
        ),
        developerMode=developer_mode,
    )


def test_get_last_reading_hides_failed_detection_by_default(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, False))

    from db import store_reading
    store_reading(None, None, "failed.jpg")

    reading = restapi_module.getLastReading()

    assert reading.id is None
    assert reading.totalconsumption is None
    assert reading.imageUrl is None


def test_get_last_reading_shows_failed_detection_in_developer_mode(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(None, None, "failed.jpg")

    reading = restapi_module.getLastReading()

    assert reading.id is not None
    assert reading.totalconsumption is None
    assert reading.imageUrl is not None
    assert reading.imageUrl.endswith("failed.jpg")


def test_get_last_reading_still_returns_successful_detection_normally(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, False))

    from db import store_reading
    store_reading(12.3, 12.3, "ok.jpg")

    reading = restapi_module.getLastReading()

    assert reading.totalconsumption == 12.3
    assert reading.imageUrl.endswith("ok.jpg")


def test_get_last_reading_distinguishes_raw_from_filtered_value(restapi_module):
    """
    totalconsumption must stay the raw OCR reading (what the meter actually
    shows), not the outlier-filtered value -- e.g. right after a confirmed
    meter replacement, negativeDeltaDetector holds `filtered` at the old
    meter's reading for a while, and the dashboard/table/photo should still
    show the new raw value, not that stale filtered one. filteredTotal
    carries the filtered value separately for callers that need it (e.g.
    the "current reading" card, which wants outlier-bereinigt).
    """
    restapi_module.storeSettings(_make_settings(restapi_module, False))

    from db import store_reading
    store_reading(0.5, 435.2, "ok.jpg")

    reading = restapi_module.getLastReading()

    assert reading.totalconsumption == 0.5
    assert reading.filteredTotal == 435.2


def test_get_last_successful_reading_ignores_trailing_failed_detection(restapi_module):
    # A single failed measurement (e.g. a watchdog reboot mid-inference)
    # must not hide the last known-good reading -- the meter reading itself
    # hasn't changed just because one photo couldn't be evaluated. This is
    # the endpoint the dashboard's status cards use, unlike /readings/last
    # which intentionally also surfaces the failed one in developer mode.
    from db import store_reading
    store_reading(12.3, 12.3, "ok.jpg")
    store_reading(None, None, "failed.jpg")

    reading = restapi_module.getLastSuccessfulReading()

    assert reading.totalconsumption == 12.3
    assert reading.imageUrl.endswith("ok.jpg")
    assert reading.isStale is False


def test_get_last_successful_reading_ignores_developer_mode(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(12.3, 12.3, "ok.jpg")
    store_reading(None, None, "failed.jpg")

    reading = restapi_module.getLastSuccessfulReading()

    assert reading.totalconsumption == 12.3


def test_get_last_successful_reading_returns_empty_when_none_exist(restapi_module):
    reading = restapi_module.getLastSuccessfulReading()

    assert reading.id is None
    assert reading.totalconsumption is None
    assert reading.isStale is False


def test_get_last_successful_reading_flags_old_value_as_stale(restapi_module):
    from db import Reading

    old_time = datetime.now() - timedelta(
        minutes=restapi_module.STALE_READING_THRESHOLD_MINUTES + 1
    )
    Reading(
        time=old_time, totalconsumption=12.3, filtered=12.3, imageName="ok.jpg"
    ).save()

    reading = restapi_module.getLastSuccessfulReading()

    assert reading.totalconsumption == 12.3
    assert reading.isStale is True


def test_get_last_successful_reading_does_not_flag_recent_value_as_stale(
    restapi_module,
):
    from db import Reading

    recent_time = datetime.now() - timedelta(
        minutes=restapi_module.STALE_READING_THRESHOLD_MINUTES - 1
    )
    Reading(
        time=recent_time, totalconsumption=12.3, filtered=12.3, imageName="ok.jpg"
    ).save()

    reading = restapi_module.getLastSuccessfulReading()

    assert reading.isStale is False


def test_get_last_readings_count_endpoint_respects_developer_mode(restapi_module):
    from db import store_reading
    store_reading(None, None, "failed.jpg")
    store_reading(1.0, 1.0, "ok.jpg")

    restapi_module.storeSettings(_make_settings(restapi_module, False))
    normal = restapi_module.getLastReadings(10)
    assert len(normal) == 1
    assert normal[0].totalconsumption == 1.0

    restapi_module.storeSettings(_make_settings(restapi_module, True))
    developer = restapi_module.getLastReadings(10)
    assert len(developer) == 2


def test_get_readings_endpoint_respects_developer_mode(restapi_module):
    from db import Reading

    now = datetime.now()
    Reading(time=now, totalconsumption=1.0, filtered=1.0, imageName="ok.jpg").save()
    Reading(
        time=now + timedelta(seconds=1),
        totalconsumption=None,
        filtered=None,
        imageName="failed.jpg",
    ).save()

    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()

    restapi_module.storeSettings(_make_settings(restapi_module, False))
    normal = restapi_module.getReadings(start, end)
    assert [r.imageUrl.split("/")[-1] for r in normal] == ["ok.jpg"]

    restapi_module.storeSettings(_make_settings(restapi_module, True))
    developer = restapi_module.getReadings(start, end)
    assert {r.imageUrl.split("/")[-1] for r in developer} == {"ok.jpg", "failed.jpg"}


def test_get_readings_includes_pipeline_stages_only_in_developer_mode(restapi_module):
    from db import store_reading

    store_reading(
        86.9672, 86.9672, "ok.jpg",
        beforeMissingNeedleCheck=86.9672,
        afterMissingDigit=86.9672, afterMaxFlow=86.9672, afterNegativeDelta=86.9672,
    )

    now = datetime.now()
    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()

    restapi_module.storeSettings(_make_settings(restapi_module, False))
    normal = restapi_module.getReadings(start, end)
    assert normal[0].pipelineStages is None

    restapi_module.storeSettings(_make_settings(restapi_module, True))
    developer = restapi_module.getReadings(start, end)
    stages = developer[0].pipelineStages
    assert stages is not None
    assert stages.rawYolo == 86.9672
    assert stages.afterMissingDigit == 86.9672
    assert stages.afterMaxFlow == 86.9672
    assert stages.afterNegativeDelta == 86.9672


def test_get_readings_pipeline_stages_reflect_a_discarded_reading(restapi_module):
    # A discarded reading (missingNeedleOrDigitDetector) has a raw value but
    # no outlier-filter values -- see db.Reading.
    from db import store_reading

    store_reading(
        None, None, "discarded.jpg",
        nNeedlesDetected=3, nDigitsDetected=5,
        discardReason="needles: expected 4, got 3.",
        beforeMissingNeedleCheck=8.9401,
    )

    now = datetime.now()
    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    stages = restapi_module.getReadings(start, end)[0].pipelineStages
    assert stages.rawYolo == 8.9401
    assert stages.afterMissingDigit is None
    assert stages.afterMaxFlow is None
    assert stages.afterNegativeDelta is None


def test_get_last_reading_falls_back_to_disk_scan_when_no_db_row_exists(
    restapi_module, images_dir
):
    # Detection can raise an exception before store_reading() is ever
    # called (see readTotalConsumption.py) -- the photo is still on disk
    # with no DB row at all. Developer mode must still surface it.
    restapi_module.storeSettings(_make_settings(restapi_module, True))
    _touch(os.path.join(images_dir, "orphaned.jpg"), time.time())

    reading = restapi_module.getLastReading()

    assert reading.id is None
    assert reading.totalconsumption is None
    assert reading.imageUrl.endswith("orphaned.jpg")


def test_get_last_reading_marks_fresh_photo_without_db_row_as_in_progress(
    restapi_module, images_dir
):
    # A photo taken moments ago with no DB row yet is (almost always)
    # simply a measurement still in progress -- a single
    # readTotalConsumption.py run takes several minutes on the Pi Zero 2 W
    # -- not an actual detection failure. Without this distinction, the
    # developer mode would show "detection failed" for the entire
    # evaluation duration of every single measurement.
    restapi_module.storeSettings(_make_settings(restapi_module, True))
    _touch(os.path.join(images_dir, "fresh.jpg"), time.time())

    reading = restapi_module.getLastReading()

    assert reading.totalconsumption is None
    assert reading.measurementInProgress is True


def test_get_last_reading_does_not_mark_old_photo_without_db_row_as_in_progress(
    restapi_module, images_dir
):
    # A photo old enough that a normal measurement run would long have
    # finished (see MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS) but still
    # has no DB row is a genuine failure (e.g. a crashed/hung run), not one
    # still in progress.
    restapi_module.storeSettings(_make_settings(restapi_module, True))
    old_epoch = time.time() - restapi_module.MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS - 60
    _touch(os.path.join(images_dir, "stale.jpg"), old_epoch)

    reading = restapi_module.getLastReading()

    assert reading.totalconsumption is None
    assert reading.measurementInProgress is False


def test_get_last_reading_marks_stale_orphaned_photo_as_crashed(
    restapi_module, images_dir
):
    # A photo old enough to no longer be "in progress" but with no DB row
    # at all means readTotalConsumption.py never reached store_reading()
    # for it -- e.g. it was killed mid-inference by the watchdog reboot
    # (see /etc/watchdog.conf) or crashed with an uncaught exception. This
    # must be distinguishable from a completed, deliberate discard by
    # missingNeedleOrDigitDetector (which DOES have a DB row with
    # debugInfo.discardReason set).
    restapi_module.storeSettings(_make_settings(restapi_module, True))
    old_epoch = time.time() - restapi_module.MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS - 60
    _touch(os.path.join(images_dir, "stale.jpg"), old_epoch)

    reading = restapi_module.getLastReading()

    assert reading.measurementCrashed is True
    assert reading.debugInfo is None


def test_get_last_reading_fresh_orphaned_photo_is_not_marked_crashed(
    restapi_module, images_dir
):
    # measurementInProgress and measurementCrashed are mutually exclusive:
    # a fresh photo without a DB row is "in progress", not "crashed".
    restapi_module.storeSettings(_make_settings(restapi_module, True))
    _touch(os.path.join(images_dir, "fresh.jpg"), time.time())

    reading = restapi_module.getLastReading()

    assert reading.measurementInProgress is True
    assert reading.measurementCrashed is False


def test_get_last_reading_completed_reading_is_never_marked_crashed(restapi_module):
    # A reading that actually completed (successfully or discarded via
    # missingNeedleOrDigitDetector) has a DB row -- it must never be
    # flagged as crashed, regardless of its age.
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(
        None, None, "discarded.jpg",
        nNeedlesDetected=3, nDigitsDetected=5,
        discardReason="needles: expected 4, got 3.",
    )

    reading = restapi_module.getLastReading()

    assert reading.measurementCrashed is False
    assert reading.debugInfo.discardReason == "needles: expected 4, got 3."


def test_get_last_reading_completed_reading_is_never_in_progress(restapi_module):
    # A reading that actually completed (successfully or discarded) must
    # never be flagged as still in progress, regardless of its age.
    restapi_module.storeSettings(_make_settings(restapi_module, False))

    from db import store_reading
    store_reading(12.3, 12.3, "ok.jpg")

    reading = restapi_module.getLastReading()

    assert reading.measurementInProgress is False


def test_get_last_reading_disk_scan_ignored_outside_developer_mode(
    restapi_module, images_dir
):
    restapi_module.storeSettings(_make_settings(restapi_module, False))
    _touch(os.path.join(images_dir, "orphaned.jpg"), time.time())

    reading = restapi_module.getLastReading()

    # No DB row at all and developer mode off -> still nothing to show,
    # the disk scan must not kick in.
    assert reading.imageUrl is None


def test_get_last_reading_prefers_newer_disk_photo_over_stale_db_row(
    restapi_module, images_dir
):
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(12.3, 12.3, "db_reading.jpg")

    # A newer photo appears on disk (e.g. the very next cron run raised an
    # exception after taking the picture) -- developer mode should surface
    # that instead of the older, successful DB row.
    _touch(os.path.join(images_dir, "newer_orphan.jpg"), time.time() + 3600)

    reading = restapi_module.getLastReading()

    assert reading.totalconsumption is None
    assert reading.imageUrl.endswith("newer_orphan.jpg")


def test_get_last_reading_keeps_db_row_when_it_is_newer_than_disk_photos(
    restapi_module, images_dir
):
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(12.3, 12.3, "db_reading.jpg")

    # An older, unrelated leftover file on disk must not shadow the
    # up-to-date DB reading.
    _touch(os.path.join(images_dir, "older_orphan.jpg"), time.time() - 3600)

    reading = restapi_module.getLastReading()

    assert reading.totalconsumption == 12.3
    assert reading.imageUrl.endswith("db_reading.jpg")
