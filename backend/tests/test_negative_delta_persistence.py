"""
Tests for negativeDeltaDetector()'s persistence check in outlierDetection.py.

A water meter can physically never decrease, so NEGDELTATHRESHOLD was
lowered from 0.1 m^3 (100 liters -- too coarse in practice, see chat
analysis of real Pi data) to a bare rounding tolerance: essentially any
decrease is now suspicious.

But treating every single decrease as an outlier to be clipped back would
be dangerous on its own: if one measurement spikes falsely HIGH (e.g. a
YOLO misclassification) and the meter's readings then continue on their
real, lower trend, every one of those genuinely-correct follow-up readings
would look like a "decrease" against the falsely-high reference and get
clipped forever.

So a decrease is only clipped back if it's isolated (the next reading
returns to roughly the old level) -- if it instead persists across several
consecutive readings, the OLD reference is judged to have been the error,
and the new, lower trend is accepted from confirmation onward.
"""
from datetime import datetime, timedelta

import pytest

NOW = datetime(2026, 1, 1, 12, 0, 0)


def seed_readings(db, readings):
    """Insert a list of (datetime, filtered_value) tuples as Reading rows."""
    for i, (time, value) in enumerate(readings):
        db.Reading.create(
            time=time,
            totalconsumption=value,
            filtered=value,
            imageName=f"dummy_{i}.jpg",
        )


class TestIsolatedDecreaseIsClippedBack:
    def test_small_isolated_decrease_below_old_threshold_is_now_caught(self, outlier_detector, db_module):
        # This is exactly the kind of small (< old 0.1 m^3) decrease that
        # used to sail through unfiltered -- must now be clipped.
        seed_readings(db_module, [(NOW - timedelta(minutes=15), 85.4890)])
        assert outlier_detector.negativeDeltaDetector(85.4489) == 85.4890

    def test_isolated_decrease_does_not_persist_stays_clipped(self, outlier_detector, db_module):
        seed_readings(db_module, [(NOW - timedelta(minutes=15), 85.489)])
        # First decrease: clipped, pending observation starts.
        assert outlier_detector.negativeDeltaDetector(85.44) == 85.489
        # Next raw measurement is a fresh, unrelated decrease far outside
        # NEGATIVE_DELTA_CONSISTENCY_TOLERANCE of the first pending value
        # (85.44) -- the pending low-value chain resets instead of
        # confirming, so this too is clipped back to the old reference.
        seed_readings(db_module, [(NOW - timedelta(minutes=0), 85.489)])
        assert outlier_detector.negativeDeltaDetector(70.0) == 85.489

    def test_within_rounding_tolerance_is_not_treated_as_decrease(self, outlier_detector, db_module):
        seed_readings(db_module, [(NOW - timedelta(minutes=15), 85.4890)])
        # Well within NEGDELTATHRESHOLD (rounding noise) -- passed through
        # unchanged, not queued as a pending decrease.
        assert outlier_detector.negativeDeltaDetector(85.48896) == 85.48896


class TestPersistentDecreaseIsAcceptedAsNewTrend:
    def test_confirmed_after_enough_consistent_readings(self, outlier_detector, db_module):
        # Simulates the real scenario this guards against: a single false
        # high spike (85.9524 -> would-be 85.9732) already got clipped by
        # an earlier filter step to some old reference; the meter then
        # genuinely continues on its real, lower trend across several
        # consecutive readings. That persistent trend must eventually win.
        seed_readings(db_module, [(NOW - timedelta(minutes=45), 85.9732)])

        # 1st decrease: clipped, matches old NEGATIVE_DELTA_CONFIRM_COUNT==3 requirement.
        assert outlier_detector.negativeDeltaDetector(85.10) == 85.9732
        # 2nd consistent low reading: still clipped (not yet confirmed).
        assert outlier_detector.negativeDeltaDetector(85.11) == 85.9732
        # 3rd consistent low reading: persistence confirmed -> new value wins.
        assert outlier_detector.negativeDeltaDetector(85.12) == 85.12

    def test_after_confirmation_value_is_used_directly_going_forward(self, outlier_detector, db_module):
        seed_readings(db_module, [(NOW - timedelta(minutes=45), 85.9732)])
        outlier_detector.negativeDeltaDetector(85.10)
        outlier_detector.negativeDeltaDetector(85.11)
        outlier_detector.negativeDeltaDetector(85.12)  # confirmed here

        pending = outlier_detector.getKeyValueStoreValue(
            outlier_detector.NEGATIVE_DELTA_PENDING_KEY, default=""
        )
        assert pending == ""

    def test_inconsistent_followup_within_the_pending_chain_resets_it(self, outlier_detector, db_module):
        # A pending chain of low values that then jumps to a very
        # different low value (outside NEGATIVE_DELTA_CONSISTENCY_TOLERANCE)
        # is noise, not a stable new trend -- must restart the count rather
        # than count towards confirmation of the original candidate.
        seed_readings(db_module, [(NOW - timedelta(minutes=45), 85.9732)])
        outlier_detector.negativeDeltaDetector(85.10)  # pending: [85.10]

        # Wildly different low value -- resets the pending chain to [80.0].
        outlier_detector.negativeDeltaDetector(80.0)

        pending = outlier_detector.getKeyValueStoreValue(
            outlier_detector.NEGATIVE_DELTA_PENDING_KEY, default=""
        )
        assert pending == "80.0"


class TestNoRegression:
    def test_no_readings_does_not_crash(self, outlier_detector):
        assert outlier_detector.negativeDeltaDetector(0.5) == 0.5

    def test_increase_is_never_touched(self, outlier_detector, db_module):
        seed_readings(db_module, [(NOW - timedelta(minutes=15), 85.0)])
        assert outlier_detector.negativeDeltaDetector(85.5) == 85.5

    def test_normal_increase_clears_any_stale_pending_state(self, outlier_detector, db_module):
        outlier_detector.setKeyValueStoreValue(
            outlier_detector.NEGATIVE_DELTA_PENDING_KEY, "84.9,84.95"
        )
        seed_readings(db_module, [(NOW - timedelta(minutes=15), 85.0)])
        outlier_detector.negativeDeltaDetector(85.5)
        pending = outlier_detector.getKeyValueStoreValue(
            outlier_detector.NEGATIVE_DELTA_PENDING_KEY, default=""
        )
        assert pending == ""
