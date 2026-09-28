"""
Tests for the meter-replacement (Zaehlertausch) detection and confirmation
flow in outlierDetection.py:

- meterReplacementDetector(): flags a suspected meter replacement (large,
  persistent downward jump) via notification + email, without altering the
  filtered pipeline value.
- confirmMeterReplacement() + the readings-since-replacement helper it
  feeds: once the user confirms, the old (high) history stops being used
  as the comparison baseline for negativeDeltaDetector/maxFlowDetector.
"""
from datetime import datetime, timedelta

import pytest

# Reading.time is unique, and several tests below derive multiple
# timestamps from a single `datetime.now()` call taken close together (e.g.
# "now - timedelta(minutes=10)" seeded in one test, "datetime.now()" seeded
# a few lines later in the same or a different test). On a system where the
# clock's effective resolution is coarser than the time between those calls,
# two such calls can land on the exact same value, tripping the unique
# constraint -- rarely, and only under certain timing, which makes it hard
# to reproduce on demand.
#
# Anchoring every test's timestamps to a single frozen instant (`NOW` below)
# with explicit, whole-minute offsets removes the system clock from the
# equation entirely: two `NOW - timedelta(minutes=n)` calls with different
# `n` can never collide, regardless of how fast the test runs or how coarse
# the underlying clock is. This avoids pulling in freezegun for what's only
# ever used to compute relative offsets, never compared against wall-clock
# time.
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


class TestMeterReplacementDetector:
    def test_no_readings_does_not_crash(self, outlier_detector):
        # Empty DB -> nothing to compare against, should be a no-op.
        outlier_detector.meterReplacementDetector(0.5)

    def test_single_large_drop_does_not_trigger_notification(self, outlier_detector, db_module):
        # One-off large drop looks the same at first as the start of a
        # meter replacement; must not fire until it persists.
        seed_readings(db_module, [(NOW - timedelta(minutes=10), 435.2)])
        outlier_detector.meterReplacementDetector(0.5)
        assert list(db_module.Notification.select()) == []

    def test_persistent_drop_triggers_notification_and_email(self, outlier_detector, db_module, monkeypatch):
        sent = []
        monkeypatch.setattr(outlier_detector, "sendEmail", lambda s, m: sent.append((s, m)))

        seed_readings(db_module, [(NOW - timedelta(minutes=10), 435.2)])

        # Same low reading recurring (simulating repeated cron runs after a
        # physical meter swap) must confirm after
        # METERREPLACEMENT_CONFIRM_COUNT consecutive observations.
        for _ in range(outlier_detector.METERREPLACEMENT_CONFIRM_COUNT - 1):
            outlier_detector.meterReplacementDetector(0.5)
            assert list(db_module.Notification.select()) == []

        outlier_detector.meterReplacementDetector(0.5)

        notifications = list(db_module.Notification.select())
        assert len(notifications) == 1
        assert notifications[0].i18nIdentifier == "meterreplacementdetected"
        assert notifications[0].type == "warning"
        assert len(sent) == 1

    def test_small_drop_within_noise_does_not_trigger(self, outlier_detector, db_module):
        seed_readings(db_module, [(NOW - timedelta(minutes=10), 100.05)])
        for _ in range(5):
            outlier_detector.meterReplacementDetector(100.0)
        assert list(db_module.Notification.select()) == []

    def test_inconsistent_followup_resets_observation(self, outlier_detector, db_module, monkeypatch):
        sent = []
        monkeypatch.setattr(outlier_detector, "sendEmail", lambda s, m: sent.append((s, m)))

        seed_readings(db_module, [(NOW - timedelta(minutes=10), 435.2)])

        # First drop looks like a replacement candidate...
        outlier_detector.meterReplacementDetector(0.5)
        # ...but the next reading jumps to something unrelated (more noise,
        # not a stable new meter reading) -> observation should restart
        # from this new value rather than count towards confirmation of
        # the original candidate.
        outlier_detector.meterReplacementDetector(200.0)

        pending = outlier_detector.getKeyValueStoreValue(
            outlier_detector.METERREPLACEMENT_PENDING_KEY, default=""
        )
        assert pending == "200.0"
        assert list(db_module.Notification.select()) == []


class TestEmptyDatabaseDoesNotCrash:
    # Regression test: on a freshly created/emptied DB (e.g. right after a
    # manual reset, or the very first measurement ever), maxFlowDetector()
    # and negativeDeltaDetector() used to index into an empty readings list
    # (readings[-1]/readings[-1].filtered) and raise IndexError. That
    # exception happened *after* a successful YOLO detection but *before*
    # store_reading() in readTotalConsumption.py's main block -- outside
    # its try/except -- so a perfectly good reading was silently lost and
    # never written to the DB. meterReplacementDetector() already guarded
    # against this; these two functions needed the same guard.
    def test_max_flow_detector_does_not_crash(self, outlier_detector):
        assert outlier_detector.maxFlowDetector(0.5) == 0.5

    def test_negative_delta_detector_does_not_crash(self, outlier_detector):
        assert outlier_detector.negativeDeltaDetector(0.5) == 0.5


class TestConfirmMeterReplacement:
    def test_confirmation_clears_pending_observation(self, outlier_detector, db_module):
        outlier_detector.setKeyValueStoreValue(
            outlier_detector.METERREPLACEMENT_PENDING_KEY, "0.5,0.6"
        )
        outlier_detector.confirmMeterReplacement()
        assert outlier_detector.getKeyValueStoreValue(
            outlier_detector.METERREPLACEMENT_PENDING_KEY, default=""
        ) == ""

    def test_negative_delta_detector_accepts_new_low_reading_after_confirmation(self, outlier_detector, db_module):
        seed_readings(db_module, [(NOW - timedelta(minutes=10), 435.2)])

        # Without confirmation, the old high reading wins: the new low
        # value is treated as an outlier and clipped back to the old one.
        assert outlier_detector.negativeDeltaDetector(0.5) == 435.2

        # confirmMeterReplacement() stamps the confirmation with the real
        # wall-clock time (it's not parameterized), so the post-confirmation
        # reading below has to be placed after that -- comfortably in the
        # future relative to the fixed NOW used for the pre-confirmation
        # reading above, which rules out any collision between the two.
        outlier_detector.confirmMeterReplacement()
        confirmedAt = outlier_detector.getLastMeterReplacement()

        # After confirmation (and no readings yet since it), the helper
        # falls back to the full history, but the new value is still what
        # actually gets read on the next real measurement once at least
        # one post-confirmation reading exists.
        seed_readings(db_module, [(confirmedAt + timedelta(minutes=1), 0.5)])
        assert outlier_detector.negativeDeltaDetector(0.55) == 0.55

    def test_max_flow_detector_ignores_pre_replacement_history(self, outlier_detector, db_module):
        seed_readings(db_module, [(NOW - timedelta(minutes=20), 435.2)])
        outlier_detector.confirmMeterReplacement()
        confirmedAt = outlier_detector.getLastMeterReplacement()
        seed_readings(db_module, [(confirmedAt + timedelta(minutes=1), 0.5)])

        # A small, plausible increase for the new meter (well within
        # MAXFLOW over 10 minutes) must not be compared against the old
        # meter's magnitude.
        assert outlier_detector.maxFlowDetector(0.51) == 0.51


class TestMaxFlowDetectorAbsoluteJump:
    """
    Regression tests for MAXFLOW_ABSOLUTE_JUMP -- production incident: a
    YOLO misread jumped the reading by 1.02 m^3 after several consecutive
    detections in between had been discarded (missing digit box), so the
    last ACCEPTED reading was 75 minutes old by the time of the bad one.
    dy/dt (1.02 / 75 ~= 0.0136 m^3/min) stayed comfortably under MAXFLOW
    (0.06 m^3/min), so the rate check alone let it through -- it was, by a
    wide margin, the single largest jump ever accepted in that meter's
    history (next largest: 0.23 m^3). maxFlowDetector() uses datetime.now()
    internally for dt, so these seed timestamps are relative to the real
    clock, not the frozen NOW used elsewhere in this file.
    """

    def test_large_jump_over_long_gap_is_still_caught(self, outlier_detector, db_module):
        # Mirrors the production case: last accepted reading well over an
        # hour ago, new value ~1 m^3 higher -- rate alone would pass this.
        last_time = datetime.now() - timedelta(minutes=75)
        seed_readings(db_module, [(last_time, 86.9499)])
        assert outlier_detector.maxFlowDetector(87.9672) == 86.9499

    def test_plausible_jump_over_long_gap_is_not_flagged(self, outlier_detector, db_module):
        # A real, if unusually large, single-interval consumption (e.g.
        # someone left a hose running) must still be accepted as long as
        # it stays under MAXFLOW_ABSOLUTE_JUMP -- this floor is meant to
        # catch reading errors, not clamp down on genuine high usage.
        last_time = datetime.now() - timedelta(minutes=75)
        seed_readings(db_module, [(last_time, 86.9499)])
        assert outlier_detector.maxFlowDetector(87.2) == 87.2

    def test_large_jump_over_short_gap_was_already_caught_by_rate(self, outlier_detector, db_module):
        # Same absolute jump, but over a normal ~10-minute interval: the
        # rate check alone already catches this (dy/dt = 1.0/10 = 0.1 >
        # MAXFLOW=0.06) -- confirms MAXFLOW_ABSOLUTE_JUMP is additive, not
        # a replacement for the existing rate check.
        last_time = datetime.now() - timedelta(minutes=10)
        seed_readings(db_module, [(last_time, 86.9499)])
        assert outlier_detector.maxFlowDetector(87.9499) == 86.9499


class TestMaxFlowDetectorPersistence:
    """
    Regression tests for MAXFLOW_PERSISTENT_CONFIRM_COUNT -- without this,
    a sustained real pipe burst would never become visible: every reading
    while the burst is ongoing gets compared against readings[-1].filtered,
    which is the last STORED value -- itself already clamped by the
    previous call, so it never advances. The stored value would freeze at
    the pre-burst reading indefinitely while the real meter kept climbing,
    and leakageDetector.py (which reads `filtered`) would never see any
    flow at all during the exact scenario detection matters most for.
    """

    def test_single_spike_is_clamped_not_passed_through(self, outlier_detector, db_module):
        # One outlier reading alone must still be clamped -- persistence
        # requires MULTIPLE consistent confirmations, not just one.
        last_time = datetime.now() - timedelta(minutes=15)
        seed_readings(db_module, [(last_time, 100.0)])
        assert outlier_detector.maxFlowDetector(101.0) == 100.0

    def test_sustained_high_flow_is_passed_through_after_confirm_count(self, outlier_detector, db_module):
        # Simulates a pipe burst: each new reading is compared against the
        # SAME stored (frozen) value, since maxFlowDetector's return value
        # here isn't written back to the DB between calls within this test
        # -- exactly mirroring how readings[-1].filtered would behave in
        # production if every call kept clamping.
        seed_readings(db_module, [(datetime.now() - timedelta(minutes=60), 100.0)])

        # First two calls: still within the pending window, clamped.
        assert outlier_detector.maxFlowDetector(101.0) == 100.0
        assert outlier_detector.maxFlowDetector(101.6) == 100.0

        # Third consistent high reading: persistence confirmed, the real
        # (high) value is passed through instead of being clamped again.
        result = outlier_detector.maxFlowDetector(102.2)
        assert result == 102.2

    def test_notification_and_email_sent_on_confirmed_high_flow(self, outlier_detector, db_module, monkeypatch):
        sent = []
        monkeypatch.setattr(outlier_detector, "sendEmail", lambda s, m: sent.append((s, m)))
        seed_readings(db_module, [(datetime.now() - timedelta(minutes=60), 100.0)])

        outlier_detector.maxFlowDetector(101.0)
        outlier_detector.maxFlowDetector(101.6)
        outlier_detector.maxFlowDetector(102.2)

        assert len(sent) == 1
        notifications = list(db_module.Notification.select())
        assert len(notifications) == 1
        assert notifications[0].i18nIdentifier == "highflowdetected"

    def test_inconsistent_outliers_do_not_accumulate_toward_persistence(self, outlier_detector, db_module):
        # Two clamped readings that DON'T agree with each other (e.g.
        # scattered OCR noise, not a real trend) must not accumulate --
        # the pending chain resets instead of confirming after 3 unrelated
        # bad readings.
        seed_readings(db_module, [(datetime.now() - timedelta(minutes=60), 100.0)])

        outlier_detector.maxFlowDetector(101.0)
        # Wildly different outlier -- more than MAXFLOW_PERSISTENT_TOLERANCE
        # away from the previous pending value, breaks the chain.
        outlier_detector.maxFlowDetector(150.0)
        result = outlier_detector.maxFlowDetector(101.5)

        # Chain was reset, so this third call only has 2 consistent
        # readings pending (150.0's break, then 101.5 restarting after
        # itself being far from 150.0) -- still clamped.
        assert result == 100.0

    def test_normal_reading_clears_pending_persistence(self, outlier_detector, db_module):
        # A brief spike followed by a return to normal must not leave a
        # stale pending chain lying around to (incorrectly) contribute
        # toward confirming some later, unrelated high reading.
        seed_readings(db_module, [(datetime.now() - timedelta(minutes=60), 100.0)])

        outlier_detector.maxFlowDetector(101.0)  # clamped, pending=[101.0]
        outlier_detector.maxFlowDetector(100.05)  # back to normal, clears pending

        # A later spike must start counting from zero again, not benefit
        # from the earlier (cleared) pending observation.
        assert outlier_detector.maxFlowDetector(101.0) == 100.0
        assert outlier_detector.maxFlowDetector(101.6) == 100.0
        # Still only the 2nd consecutive confirmation since the reset --
        # not yet MAXFLOW_PERSISTENT_CONFIRM_COUNT (3).
