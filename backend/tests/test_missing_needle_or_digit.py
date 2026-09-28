"""
Tests for outlierDetection.missingNeedleOrDigitDetector() / _learnExpectedBoxCount():

YOLO can fail to detect one of the needle or digit boxes in a given photo
(e.g. a poorly lit/partially occluded needle falling below the confidence
threshold). getIntegerFromPredictions() (readTotalConsumption.py) just
concatenates whatever boxes it *did* find into a digit string, so a missing
box silently shifts every remaining digit by a power of ten instead of
producing a merely-slightly-off value. This can't be recovered from the
digit sequence itself -- only from the box *count* -- so the expected count
is learned from history (the mode of the last successful detections) and a
current detection with fewer boxes than that learned mode is flagged.
"""
from datetime import datetime, timedelta

import pytest

NOW = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture(autouse=True)
def _enabled(outlier_detector, monkeypatch):
    """
    The detector ships enabled by default (see
    MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED) -- force it on explicitly for
    every test in this file regardless of that default, so these tests keep
    covering the underlying logic even if the default is later flipped back
    off (see test_disabled_is_a_noop below for that kill-switch path).
    """
    monkeypatch.setattr(outlier_detector, "MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED", True)


def seed_readings_with_counts(db, n, nNeedles=4, nDigits=5, start_minutes_ago=100):
    """
    Insert n successful Reading rows, evenly spaced backwards from NOW,
    all with the given nNeedlesDetected/nDigitsDetected.
    """
    for i in range(n):
        db.Reading.create(
            time=NOW - timedelta(minutes=start_minutes_ago - i),
            totalconsumption=float(i),
            filtered=float(i),
            imageName=f"dummy_{i}.jpg",
            nNeedlesDetected=nNeedles,
            nDigitsDetected=nDigits,
        )


class TestLearnExpectedBoxCount:
    def test_not_enough_history_returns_none(self, outlier_detector, db_module):
        # Fewer rows than NEEDLE_DIGIT_COUNT_MIN_HISTORY -> can't trust a mode yet.
        seed_readings_with_counts(db_module, 3, nNeedles=4)
        assert outlier_detector._learnExpectedBoxCount("nNeedlesDetected") is None

    def test_learns_the_mode_of_recent_history(self, outlier_detector, db_module):
        seed_readings_with_counts(db_module, 12, nNeedles=4)
        assert outlier_detector._learnExpectedBoxCount("nNeedlesDetected") == 4

    def test_rows_without_the_field_are_ignored(self, outlier_detector, db_module):
        # Old rows from before these columns existed have NULL there --
        # they must not count towards (or dilute) the learned mode.
        for i in range(12):
            db_module.Reading.create(
                time=NOW - timedelta(minutes=100 - i),
                totalconsumption=float(i),
                filtered=float(i),
                imageName=f"legacy_{i}.jpg",
                nNeedlesDetected=None,
                nDigitsDetected=None,
            )
        assert outlier_detector._learnExpectedBoxCount("nNeedlesDetected") is None


class TestMissingNeedleOrDigitDetector:
    def test_no_history_does_not_flag_and_does_not_crash(self, outlier_detector, db_module):
        # Fresh install: nothing learned yet -> nothing to compare against.
        assert outlier_detector.missingNeedleOrDigitDetector(3, 5) is None
        assert list(db_module.Notification.select()) == []

    def test_matching_count_is_not_flagged(self, outlier_detector, db_module):
        seed_readings_with_counts(db_module, 12, nNeedles=4, nDigits=5)
        assert outlier_detector.missingNeedleOrDigitDetector(4, 5) is None
        assert list(db_module.Notification.select()) == []

    def test_more_than_expected_is_not_flagged(self, outlier_detector, db_module):
        # Only a shortfall is suspicious (a missing box); an extra
        # detection isn't the failure mode this guards against.
        seed_readings_with_counts(db_module, 12, nNeedles=4, nDigits=5)
        assert outlier_detector.missingNeedleOrDigitDetector(5, 5) is None

    def test_fewer_needles_than_learned_is_flagged_but_does_not_notify(self, outlier_detector, db_module, caplog):
        # Returns the discard reason as a string (so the reading gets
        # discarded and the reason can be stored in Reading.discardReason
        # for the developer mode, see restapi.py/CardLastPhoto.jsx) and
        # logs a warning for diagnosis, but must NOT surface a user-facing
        # Notification -- the frontend's bell dropdown (see
        # DropdownNotification.jsx) is reserved for things that actually
        # matter to the user (meter replacement, leak suspicion), not every
        # internally-handled detection glitch of this defensive filter.
        seed_readings_with_counts(db_module, 12, nNeedles=4, nDigits=5)
        with caplog.at_level("WARNING"):
            reason = outlier_detector.missingNeedleOrDigitDetector(3, 5)
        assert reason is not None
        assert "needles" in reason
        assert list(db_module.Notification.select()) == []
        assert any("needles" in record.message for record in caplog.records)

    def test_fewer_digits_than_learned_is_flagged(self, outlier_detector, db_module):
        seed_readings_with_counts(db_module, 12, nNeedles=4, nDigits=5)
        assert outlier_detector.missingNeedleOrDigitDetector(4, 4) is not None

    def test_both_fewer_reports_both_in_reason_without_notifying(self, outlier_detector, db_module, caplog):
        seed_readings_with_counts(db_module, 12, nNeedles=4, nDigits=5)
        with caplog.at_level("WARNING"):
            reason = outlier_detector.missingNeedleOrDigitDetector(3, 4)
        assert reason is not None
        assert "needles" in reason and "digits" in reason
        assert list(db_module.Notification.select()) == []


def test_disabled_is_a_noop(outlier_detector, db_module, monkeypatch):
    # Overrides the file-wide _enabled autouse fixture for this one test to
    # verify the kill-switch itself still works: with the flag off, a
    # shortfall that would otherwise be flagged must be silently ignored,
    # with no notification and no history lookup required.
    monkeypatch.setattr(outlier_detector, "MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED", False)
    seed_readings_with_counts(db_module, 12, nNeedles=4, nDigits=5)
    assert outlier_detector.missingNeedleOrDigitDetector(1, 1) is None
    assert list(db_module.Notification.select()) == []


def test_enabled_by_default(outlier_detector):
    # The detector ships enabled -- this is what actually runs on the Pi
    # unless someone flips the flag back off.
    assert outlier_detector.MISSING_NEEDLE_OR_DIGIT_DETECTOR_ENABLED is True
