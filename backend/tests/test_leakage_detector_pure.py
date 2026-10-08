"""
Tests for the pure, DB-free computations in leakageDetector.py:
- estimate_noise_threshold
- compute_robust_flow
- find_sustained_high_flow
- select_leak_signal_readings
- compute_flow_features
- find_stable_periods_dual_check
- compute_zscore_anomalies
- compute_isolation_forest_anomalies

These take pandas Series/DataFrames directly, so they're tested without
touching the database, email, or MQTT layers.
"""
import numpy as np
import pandas as pd
import pytest

from leakageDetector import (
    estimate_noise_threshold,
    compute_robust_flow,
    find_sustained_high_flow,
    select_leak_signal_readings,
    DAYTIME_MIN_CONSECUTIVE_ANOMALIES,
    QUIET_HOURS_MIN_ANOMALIES,
    compute_flow_features,
    find_stable_periods_dual_check,
    compute_zscore_anomalies,
    compute_isolation_forest_anomalies,
    count_baseline_days,
    MIN_NOISE_THRESHOLD,
    GAP_THRESHOLD_HOURS,
    ZSCORE_MIN_SLOT_OBSERVATIONS,
)


def make_series(values, start="2026-08-01 00:00:00", freq="15min"):
    idx = pd.date_range(start, periods=len(values), freq=freq)
    return pd.Series(values, index=idx, dtype=float)


def make_household_series(n_days, leak_start_hour=None, leak_end_hour=None,
                           leak_per_slot=0.0, seed=42, start="2026-07-01"):
    """
    Build n_days of synthetic 15-minute meter readings (matching
    leakageDetector.SLOT_MINUTES): quiet nights and occasional daytime
    consumption bursts throughout, plus -- if a leak window is given -- a
    steady leak injected into *only the last day's* [leak_start_hour,
    leak_end_hour) window.

    The leak is confined to the last day deliberately: a slow drip that
    has already been running for the entire lookback window is, by
    definition, what these models learn as "normal" for that time slot,
    so it can never be flagged as anomalous by design. Detecting a leak
    that is new relative to the household's established baseline is
    exactly what these tests are meant to exercise.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n_days * 96, freq="15min")
    last_day_start = idx[-1] - pd.Timedelta(hours=24)
    value = 100.0
    values = []
    for ts in idx:
        flow = 0.0
        if 6 <= ts.hour < 23 and rng.random() < 0.15:
            flow = abs(rng.normal(0.01, 0.002))
        if leak_start_hour is not None and ts > last_day_start and leak_start_hour <= ts.hour < leak_end_hour:
            flow += leak_per_slot
        value += flow
        values.append(value)
    return pd.Series(values, index=idx, dtype=float)


# ---------------------------------------------------------------------------
# estimate_noise_threshold
# ---------------------------------------------------------------------------

class TestEstimateNoiseThreshold:
    def test_all_flat_returns_floor(self):
        ts = make_series([100.0] * 50)
        assert estimate_noise_threshold(ts) == MIN_NOISE_THRESHOLD

    def test_empty_diffs_returns_floor(self):
        ts = make_series([100.0])  # single point -> diff() is all-NaN after dropna
        assert estimate_noise_threshold(ts) == MIN_NOISE_THRESHOLD

    def test_floor_is_respected(self):
        # Tiny jitter should not push the threshold below the floor.
        ts = make_series([100.0, 100.00001, 100.00002, 100.00001, 100.00003])
        assert estimate_noise_threshold(ts) >= MIN_NOISE_THRESHOLD

    def test_large_jitter_exceeds_floor(self):
        # Consumption steps with substantial jitter should push the
        # estimate above the floor.
        values = [100.0]
        for step in [0.5, 0.05, 0.52, 0.06, 0.55, 0.04, 0.51, 0.07] * 5:
            values.append(values[-1] + step)
        ts = make_series(values)
        threshold = estimate_noise_threshold(ts, min_threshold=0.0)
        assert threshold > 0.0

    def test_ignores_negative_diffs_when_estimating(self):
        # Negative diffs (which shouldn't occur on a monotonic meter, but
        # the function should be robust if they slip through) must not
        # be used to compute the noise scale.
        ts = make_series([100.0, 99.0, 100.0, 99.0, 100.0])
        assert estimate_noise_threshold(ts) == MIN_NOISE_THRESHOLD

    def test_custom_min_threshold_and_k(self):
        ts = make_series([100.0] * 10)
        assert estimate_noise_threshold(ts, k=10.0, min_threshold=0.5) == 0.5


# ---------------------------------------------------------------------------
# compute_flow_features
# ---------------------------------------------------------------------------

class TestComputeRobustFlow:
    def test_monotonic_series_equals_plain_diff(self):
        ts = make_series([100.0, 100.5, 100.5, 101.0, 101.2])
        flow = compute_robust_flow(ts)
        assert flow.iloc[1:].tolist() == pytest.approx([0.5, 0.0, 0.5, 0.2])
        assert pd.isna(flow.iloc[0])

    def test_too_high_misread_that_is_taken_back_is_not_flow(self):
        # Production case: the 0.1 m^3 digit flipping at night.
        ts = make_series([93.8936, 93.9936, 93.8936, 93.9946, 93.8946, 93.8946])
        flow = compute_robust_flow(ts)
        assert flow.iloc[1:].tolist() == pytest.approx([0.0, 0.0, 0.001, 0.0, 0.0])

    def test_rebound_after_too_low_misread_is_not_flow(self):
        # Production case: two too-low readings in a row, then back.
        ts = make_series([95.0818, 95.0818, 94.9490, 94.9511, 95.0861, 95.0861])
        flow = compute_robust_flow(ts)
        assert flow.iloc[1:].tolist() == pytest.approx([0.0, 0.0, 0.0, 0.0043, 0.0])

    def test_real_flow_right_after_a_misread_is_kept(self):
        ts = make_series([100.0, 100.1, 100.0, 100.0, 100.0, 100.3, 100.3])
        flow = compute_robust_flow(ts)
        assert flow.iloc[5] == pytest.approx(0.3)
        assert flow.sum() == pytest.approx(0.3)

    def test_misread_longer_than_window_counts_as_flow(self):
        ts = make_series([100.0, 100.1, 100.1, 100.1, 100.0, 100.0])
        assert compute_robust_flow(ts, window=2).iloc[1] == pytest.approx(0.1)
        assert compute_robust_flow(ts, window=3).iloc[1] == pytest.approx(0.0)


class TestFindSustainedHighFlow:
    def test_flat_series_is_not_flagged(self):
        assert find_sustained_high_flow(make_series([100.0] * 10)) is None

    def test_burst_over_four_readings_is_flagged(self):
        # 75 L per 15 min = 300 L/h, four intervals in a row.
        ts = make_series([100.0, 100.0, 100.0, 100.075, 100.15, 100.225, 100.3])
        assert find_sustained_high_flow(ts) == pytest.approx(0.3)

    def test_short_large_draw_is_not_flagged(self):
        # A shower: high rate, but only for two readings.
        ts = make_series([100.0, 100.0, 100.0, 100.0, 100.0, 100.11, 100.15])
        assert find_sustained_high_flow(ts) is None

    def test_slow_flow_is_not_flagged(self):
        # 12 L/h: sustained, but far below the rate threshold.
        ts = make_series([100.0 + i * 0.003 for i in range(10)])
        assert find_sustained_high_flow(ts) is None

    def test_flipping_digit_is_not_flagged(self):
        ts = make_series([100.0, 100.1, 100.0, 100.1, 100.0, 100.1, 100.0, 100.1])
        assert find_sustained_high_flow(ts) is None

    def test_too_short_series_is_not_flagged(self):
        assert find_sustained_high_flow(make_series([100.0, 100.1, 100.2])) is None
        assert find_sustained_high_flow(make_series([])) is None


class TestSelectLeakSignalReadings:
    @staticmethod
    def result(start, flags, flows=None):
        idx = pd.date_range(start, periods=len(flags), freq="15min")
        if flows is None:
            flows = [0.01] * len(flags)
        return pd.DataFrame({"is_anomaly": flags, "flow": flows}, index=idx)

    def test_single_flag_at_night_does_not_count(self):
        r = self.result("2026-08-02 02:00", [False, True, False])
        assert select_leak_signal_readings(r).empty

    def test_several_flags_at_night_count_even_if_not_in_a_row(self):
        flags = [True, False] * QUIET_HOURS_MIN_ANOMALIES
        r = self.result("2026-08-02 01:00", flags)
        assert len(select_leak_signal_readings(r)) == QUIET_HOURS_MIN_ANOMALIES

    def test_short_daytime_run_does_not_count(self):
        flags = [False] + [True] * (DAYTIME_MIN_CONSECUTIVE_ANOMALIES - 1) + [False]
        r = self.result("2026-08-02 10:00", flags)
        assert select_leak_signal_readings(r).empty

    def test_long_daytime_run_counts(self):
        flags = [False] + [True] * DAYTIME_MIN_CONSECUTIVE_ANOMALIES + [False]
        r = self.result("2026-08-02 10:00", flags)
        assert len(select_leak_signal_readings(r)) == DAYTIME_MIN_CONSECUTIVE_ANOMALIES

    def test_separate_short_daytime_runs_are_not_added_up(self):
        r = self.result("2026-08-02 10:00", [True, True, False, True, True, False, True])
        assert select_leak_signal_readings(r).empty

    def test_flagged_readings_without_own_flow_do_not_count(self):
        # One draw at night, flagged three times in a row via the rolling
        # sum, and the same during the day stretched to a "run" of four.
        night = self.result("2026-08-02 01:00", [True, True, True], flows=[0.1, 0.0, 0.0])
        assert select_leak_signal_readings(night).empty
        day = self.result("2026-08-02 10:00", [True] * 4, flows=[0.1, 0.0, 0.1, 0.0])
        assert select_leak_signal_readings(day).empty

    def test_daytime_flags_do_not_count_towards_the_night_minimum(self):
        # 04:30 and 04:45 are inside the quiet hours, 05:00 is not.
        r = self.result("2026-08-02 04:30", [True, True, True])
        assert select_leak_signal_readings(r).empty

    def test_empty_frame_passes_through(self):
        assert select_leak_signal_readings(pd.DataFrame()).empty


class TestComputeFlowFeatures:
    def test_flow_is_nonneg_diff(self):
        ts = make_series([100.0, 100.5, 100.5, 101.0])
        features = compute_flow_features(ts)
        assert list(features['flow']) == [0.5, 0.0, 0.5]

    def test_negative_deltas_are_clipped_to_zero(self):
        ts = make_series([100.0, 99.0, 99.5])
        features = compute_flow_features(ts)
        assert (features['flow'] >= 0).all()
        assert features['flow'].iloc[0] == 0.0

    def test_active_flow_streak_resets_on_zero_flow(self):
        ts = make_series([100.0, 100.5, 101.0, 101.0, 101.5, 102.0])
        features = compute_flow_features(ts)
        # diffs: 0.5, 0.5, 0.0, 0.5, 0.5 -> streaks: 1, 2, 0, 1, 2
        assert list(features['active_flow_streak']) == [1, 2, 0, 1, 2]

    def test_rolling_sum_30m_window(self):
        # With gapless 15-minute sampling, a 30-minute time-based window
        # happens to cover the same <=2 trailing readings a row-count
        # window of size 2 would -- see test_rolling_sum_30m_is_time_based_
        # not_row_count_based below for the case where that stops holding.
        ts = make_series([100.0, 100.1, 100.2, 100.3, 100.4])
        features = compute_flow_features(ts)
        # flows: 0.1, 0.1, 0.1, 0.1
        # rolling sum over the trailing 30 minutes: [0.1, 0.2, 0.2, 0.2]
        assert features['rolling_sum_30m'].round(5).tolist() == [0.1, 0.2, 0.2, 0.2]

    def test_rolling_sum_30m_is_time_based_not_row_count_based(self):
        # Regression test: rolling_sum_30m must sum flow that actually
        # occurred within the trailing 30 minutes of wall-clock time, not
        # just "the last N rows" -- readings are not reliably equidistant
        # (a failed detection, a discarded outlier, a watchdog reboot mid-
        # inference, ... simply produces no row for that slot). A
        # row-count window would let flow from readings hours apart land in
        # what it believes is a 30-minute window, inflating rolling_sum_30m
        # right after a gap and (via the same feature, reused as training
        # data) corrupting that time-slot's baseline statistics in
        # compute_zscore_anomalies/compute_isolation_forest_anomalies --
        # this produced a real false-positive leak warning in production.
        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-01-01 03:00:00"),
            pd.Timestamp("2026-01-01 03:15:00"),
            # A 2-hour gap: several 15-minute slots in between failed to
            # detect a reading at all, well under GAP_THRESHOLD_HOURS so
            # this is NOT treated as a measurement outage -- it's exactly
            # the "some slots missing but not a real outage" case a
            # row-count window mishandles. Only ONE reading follows the
            # gap here so a row-count window of 2 has no choice but to
            # reach back across the gap to the pre-gap reading.
            pd.Timestamp("2026-01-01 05:15:00"),
        ])
        # Small, real flow at each step -- nothing here should ever be
        # mistaken for a 2-hour-worth-of-flow spike.
        ts = pd.Series([100.0, 100.01, 100.02], index=idx, dtype=float)
        features = compute_flow_features(ts)
        # flows (after dropping the first NaN row): 0.01, 0.01
        # A row-count window of 2 would sum the 03:15 and 05:15 readings
        # together at the 05:15 row (0.02), even though they're 2 hours
        # apart. The time-based window must only ever see the current
        # reading there, since nothing else falls within the trailing 30
        # minutes of wall-clock time.
        assert features['rolling_sum_30m'].round(5).tolist() == pytest.approx([0.01, 0.01])

    def test_hour_and_minute_of_day(self):
        # The first reading is dropped (its flow is NaN, nothing to diff
        # against), so the surviving row is the *second* timestamp, 15
        # minutes after the series start (03:35). minute_of_day floors to
        # the SLOT_MINUTES=15 bucket (03:30), not the exact minute -- see
        # test_minute_of_day_buckets_to_slot_minutes below for why.
        ts = make_series([100.0, 100.1], start="2026-08-01 03:20:00")
        features = compute_flow_features(ts)
        assert features['hour_of_day'].iloc[0] == 3
        assert features['minute_of_day'].iloc[0] == 3 * 60 + 30

    def test_minute_of_day_buckets_to_slot_minutes(self):
        # Regression test: minute_of_day must floor to the nearest
        # SLOT_MINUTES bucket rather than use the exact minute. Across
        # different days, readings "at" the same nominal time-of-day jitter
        # by a few seconds/minutes (camera-trigger timing, variable
        # detection-pipeline duration) -- e.g. three consecutive nights'
        # ~06:04 reading landed at :04:11, :04:29, :04:36 (same exact-minute
        # slot, 364, by coincidence) but the fourth landed at :04:42 (a
        # DIFFERENT exact-minute slot, 365). Grouping by exact minute-of-day
        # in compute_zscore_anomalies would therefore scatter what should be
        # one "around 06:00" sample across near-singleton groups -- each
        # with a near-zero std purely from having only 1-2 historical
        # points, trivially breached by ordinary jitter and producing a
        # spurious triple-digit z-score on flat, unremarkable data (this
        # happened in production). All four timestamps below are within one
        # SLOT_MINUTES=15 bucket of each other and must map to the same
        # minute_of_day.
        ts = make_series(
            [100.0, 100.1, 100.2, 100.3, 100.4],
            start="2026-08-01 06:04:11", freq="10s",
        )
        features = compute_flow_features(ts)
        assert features['minute_of_day'].nunique() == 1
        assert features['minute_of_day'].iloc[0] == 6 * 60

    def test_drops_first_row_with_nan_flow(self):
        ts = make_series([100.0, 100.1, 100.2])
        features = compute_flow_features(ts)
        assert len(features) == len(ts) - 1

    def test_long_gap_does_not_produce_a_flow_spike(self):
        # A months-long outage between two readings must not show up as a
        # single flow reading equal to the entire consumption accumulated
        # during the outage -- the row spanning the gap should be dropped
        # entirely, just like the very first reading of a series is.
        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-01-01 00:00:00"),
            pd.Timestamp("2026-01-01 00:10:00"),
            pd.Timestamp("2026-07-01 00:00:00"),  # ~6 months later
            pd.Timestamp("2026-07-01 00:10:00"),
        ])
        ts = pd.Series([100.0, 100.1, 150.0, 150.05], index=idx, dtype=float)
        features = compute_flow_features(ts)
        assert 150.0 not in features['value'].values or (features['flow'] < 10).all()
        assert list(features.index) == [idx[1], idx[3]]
        assert features['flow'].tolist() == pytest.approx([0.1, 0.05])

    def test_gap_resets_active_flow_streak(self):
        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-01-01 00:00:00"),
            pd.Timestamp("2026-01-01 00:10:00"),
            pd.Timestamp("2026-07-01 00:00:00"),
            pd.Timestamp("2026-07-01 00:10:00"),
        ])
        ts = pd.Series([100.0, 100.1, 150.0, 150.05], index=idx, dtype=float)
        features = compute_flow_features(ts)
        # The post-gap reading starts its own streak, unaffected by the
        # (dropped) gap row.
        assert list(features['active_flow_streak']) == [1, 1]

    def test_short_gap_is_not_treated_as_outage(self):
        # A gap just under the threshold is ordinary sampling behaviour,
        # not an outage -- it must be diffed normally.
        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-01-01 00:00:00"),
            pd.Timestamp("2026-01-01 00:10:00"),
            pd.Timestamp("2026-01-01 00:10:00") + pd.Timedelta(hours=GAP_THRESHOLD_HOURS - 1),
        ])
        ts = pd.Series([100.0, 100.1, 100.2], index=idx, dtype=float)
        features = compute_flow_features(ts)
        assert len(features) == 2
        assert features['flow'].tolist() == pytest.approx([0.1, 0.1])


# ---------------------------------------------------------------------------
# count_baseline_days
# ---------------------------------------------------------------------------

class TestCountBaselineDays:
    def test_counts_calendar_days_with_readings(self):
        # 3 days of gapless 15-minute readings starting at midnight.
        assert count_baseline_days(make_series([100.0] * (3 * 96))) == 3

    def test_days_inside_an_outage_are_not_counted(self):
        idx = pd.to_datetime(["2026-03-01 10:00", "2026-03-01 10:15", "2026-03-20 10:00"])
        assert count_baseline_days(pd.Series([1.0, 1.0, 2.0], index=idx)) == 2

    def test_empty_series(self):
        assert count_baseline_days(make_series([])) == 0


# ---------------------------------------------------------------------------
# find_stable_periods_dual_check
# ---------------------------------------------------------------------------

class TestFindStablePeriodsDualCheck:
    def test_raises_on_non_datetime_index(self):
        s = pd.Series([1, 2, 3])
        with pytest.raises(TypeError):
            find_stable_periods_dual_check(s)

    def test_raises_on_non_numeric_values(self):
        idx = pd.date_range("2026-08-01", periods=3, freq="15min")
        s = pd.Series(["a", "b", "c"], index=idx)
        with pytest.raises(TypeError):
            find_stable_periods_dual_check(s)

    def test_empty_series_returns_empty(self):
        s = make_series([])
        assert find_stable_periods_dual_check(s) == []

    def test_single_point_returns_empty(self):
        s = make_series([100.0])
        assert find_stable_periods_dual_check(s) == []

    def test_finds_flat_period_covering_whole_series(self):
        s = make_series([100.0] * 10)
        result = find_stable_periods_dual_check(s, threshold=0, minDuration=0)
        assert len(result) == 1
        assert result[0]['change_within_interval'] == 0.0

    def test_no_stable_period_when_constantly_increasing(self):
        s = make_series([100.0 + i * 0.1 for i in range(10)])
        result = find_stable_periods_dual_check(s, threshold=0, minDuration=0)
        assert result == []

    def test_finds_stable_period_between_bursts(self):
        # Rises, then flat for a while, then rises again.
        values = [100.0, 100.5, 101.0] + [101.0] * 8 + [101.5, 102.0]
        s = make_series(values)
        result = find_stable_periods_dual_check(s, threshold=0, minDuration=0)
        assert len(result) == 1
        assert result[0]['change_within_interval'] == 0.0
        # The stable segment runs from the last rising point (index 2,
        # value 101.0) through the 8 explicitly-flat readings (index 10)
        # -- 9 points, 8 steps of 15 min each = 120 min.
        assert result[0]['duration_hours'] == pytest.approx(120 / 60, rel=1e-6)

    def test_min_duration_filters_short_stable_periods(self):
        # A 30-minute flat period (3 points, 15 min apart) should be
        # filtered out by a 1-hour minimum duration requirement.
        values = [100.0, 100.5, 100.5, 100.5, 101.0]
        s = make_series(values)
        result = find_stable_periods_dual_check(s, threshold=0, minDuration=1.0)
        assert result == []

    def test_threshold_allows_small_fluctuation(self):
        # Values wiggle within +/- 0.001 of each other; with a threshold of
        # 0.002 this should count as one stable period.
        values = [100.0, 100.001, 100.0005, 100.0015, 100.0]
        s = make_series(values)
        result = find_stable_periods_dual_check(s, threshold=0.002, minDuration=0)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# compute_zscore_anomalies
# ---------------------------------------------------------------------------

class TestComputeZscoreAnomalies:
    def test_empty_baseline_returns_empty(self):
        s = make_series([])
        result = compute_zscore_anomalies(s)
        assert result.empty

    def test_normal_pattern_yields_few_anomalies(self):
        # make_household_series places each daytime burst independently
        # with 15% probability per 15-minute slot, so two bursts landing in
        # the same 30-minute rolling_sum_30m window (a real, if
        # infrequent, ~2% per opportunity coincidence) is not a defect in
        # the data -- it genuinely is a somewhat-unusual reading relative
        # to typical single-burst slots, and MAD (unlike a floored std that
        # would swallow it) is expected to catch some of these. Assert a
        # bounded rate rather than "close to zero": this isn't scoring
        # against a normal distribution's tail, it's scoring against a
        # zero-inflated one where a double-burst really is an outlier.
        s = make_household_series(n_days=14)
        result = compute_zscore_anomalies(s)
        assert not result.empty
        assert result['is_anomaly'].sum() <= len(result) * 0.15

    def test_injected_night_leak_is_detected(self):
        # 0.003 m^3/15min (~12 L/h): rolling_sum_30m for this leak
        # (2 * 0.003 = 0.006 m^3) needs to clear min_std=0.002 by a
        # comfortable margin, since night slots typically have zero flow in
        # their history and therefore MAD == 0, floored to min_std -- a
        # leak much smaller than that floor would be statistically
        # indistinguishable from ordinary jitter, as it should be.
        s = make_household_series(
            n_days=14, leak_start_hour=1, leak_end_hour=5, leak_per_slot=0.003,
        )
        result = compute_zscore_anomalies(s)
        assert not result.empty
        night_anomalies = result[(result['is_anomaly']) & (result['hour_of_day'].between(1, 4))]
        assert len(night_anomalies) > 0

    def test_evaluated_day_excluded_from_its_own_baseline(self):
        # Regression test: including the last day in its own baseline
        # statistics let a leak inflate the mean/std it was compared
        # against, hiding it. Build a baseline where every day *except*
        # the most recent one is leak-free, then confirm the leak in the
        # last day is still flagged (i.e. history stats aren't polluted
        # by peeking at the day being scored).
        clean_days = make_household_series(n_days=13, seed=1)
        leaky_last_day = make_household_series(
            n_days=1, leak_start_hour=1, leak_end_hour=5, leak_per_slot=0.0005,
            seed=2, start=clean_days.index[-1] + pd.Timedelta(minutes=15),
        )
        # Re-base the leaky day onto the end of the clean series so the
        # meter reading stays monotonic across the join.
        offset = clean_days.iloc[-1] - 100.0
        leaky_last_day = leaky_last_day + offset
        combined = pd.concat([clean_days, leaky_last_day])

        result = compute_zscore_anomalies(combined)
        assert not result.empty
        assert result['is_anomaly'].sum() > 0

    def test_insufficient_history_returns_empty(self):
        # Fewer than ZSCORE_MIN_SLOT_OBSERVATIONS+1 days -> no slot has
        # enough history to be trusted (this series has exactly one
        # observation per slot per day, so day count == observations per
        # slot in its history).
        s = make_household_series(n_days=1)
        result = compute_zscore_anomalies(s)
        assert result.empty

    def test_leak_during_warmup_is_not_flagged_by_zscore_alone(self):
        # Documents a deliberate trade-off (see ZSCORE_MIN_SLOT_OBSERVATIONS):
        # during the first ~10 days after a fresh install (or after losing
        # history to a gap), the Z-score model won't flag ANYTHING for a
        # slot with too little history yet -- including a real leak, not
        # just false positives. This is accepted rather than worked around
        # because detectLeakage()'s other two signals (find_stable_periods_
        # dual_check, Isolation Forest) don't depend on any single slot's
        # history and are expected to catch a leak like this during
        # warm-up on their own.
        s = make_household_series(
            n_days=ZSCORE_MIN_SLOT_OBSERVATIONS,
            leak_start_hour=1, leak_end_hour=5, leak_per_slot=0.003,
        )
        result = compute_zscore_anomalies(s)
        assert result.empty

    def test_only_evaluates_most_recent_24h(self):
        # Needs > ZSCORE_MIN_SLOT_OBSERVATIONS days so every slot's history
        # (n_days - 1, since the most recent day is held out for scoring)
        # actually clears that threshold.
        s = make_household_series(n_days=ZSCORE_MIN_SLOT_OBSERVATIONS + 2)
        result = compute_zscore_anomalies(s)
        span = result.index.max() - result.index.min()
        assert span <= pd.Timedelta(hours=24)


class TestZscoreUsualMax:
    @staticmethod
    def series_with_daily_draw(n_days, slot_of_day, last_day_slot):
        """
        n_days of flat 15-minute readings with one 50 L draw per day: at
        slot_of_day(day) on the history days, at last_day_slot on the last.
        """
        values, level = [], 100.0
        for day in range(n_days):
            draw_slot = last_day_slot if day == n_days - 1 else slot_of_day(day)
            for slot in range(96):
                if slot == draw_slot:
                    level += 0.05
                values.append(level)
        return make_series(values, start="2026-07-01 00:00:00")

    def test_draw_usual_for_the_time_of_day_is_not_flagged_in_a_new_slot(self):
        # History: a draw every day somewhere between 08:00 and 09:45, but
        # never at 10:00 -- which is where it lands on the evaluated day.
        ts = self.series_with_daily_draw(20, lambda day: 32 + day % 8, last_day_slot=40)
        result = compute_zscore_anomalies(ts)
        assert not result['is_anomaly'].any()

    def test_same_draw_at_an_always_quiet_time_of_day_is_flagged(self):
        ts = self.series_with_daily_draw(20, lambda day: 32 + day % 8, last_day_slot=80)
        result = compute_zscore_anomalies(ts)
        assert result['is_anomaly'].any()


# ---------------------------------------------------------------------------
# compute_isolation_forest_anomalies
# ---------------------------------------------------------------------------

class TestComputeIsolationForestAnomalies:
    def test_empty_baseline_returns_empty(self):
        s = make_series([])
        result = compute_isolation_forest_anomalies(s)
        assert result.empty

    def test_insufficient_history_returns_empty(self):
        s = make_household_series(n_days=1)
        result = compute_isolation_forest_anomalies(s)
        assert result.empty

    def test_normal_pattern_yields_few_anomalies(self):
        s = make_household_series(n_days=14)
        result = compute_isolation_forest_anomalies(s, contamination=0.02)
        assert not result.empty
        # contamination=0.02 over one day (96 rows) -> expect a handful at most.
        assert result['is_anomaly'].sum() <= 6

    def test_injected_night_leak_is_detected(self):
        # Isolation Forest scores points by how unusual their *combination*
        # of features is; a leak has to be large enough to actually stand
        # out from the spread of ordinary daytime consumption bursts baked
        # into the same training data. 0.002 m^3/15min (~8 L/h) matches a
        # dripping valve / running toilet, comfortably above that noise floor.
        s = make_household_series(
            n_days=14, leak_start_hour=1, leak_end_hour=5, leak_per_slot=0.002,
        )
        result = compute_isolation_forest_anomalies(s, contamination=0.02)
        assert not result.empty
        night_anomalies = result[(result['is_anomaly']) & (result['hour_of_day'].between(1, 4))]
        assert len(night_anomalies) > 0

    def test_all_zero_flow_household_is_never_flagged(self):
        # Regression test: contamination is a PROPORTION ("flag ~2% of
        # points"), not a physical threshold -- IsolationForest will always
        # call *something* the most unusual point relative to the rest of
        # its training data, even when every point is physically
        # meaningless. A near-all-zero-flow household with only rare,
        # tiny (sub-noise-floor) jitter reproduced this in practice: a
        # single 1.4 ml reading got flagged purely for being statistically
        # atypical among an otherwise dead-flat 40-day history.
        # min_flagged_volume exists specifically so a flagged point must
        # also clear a real physical volume before counting as an anomaly.
        rng = np.random.default_rng(11)
        idx = pd.date_range("2026-07-01", periods=40 * 96, freq="15min")
        value = 100.0
        values = []
        for _ in idx:
            # Sporadic, tiny, sub-MIN_NOISE_THRESHOLD jitter on ~5% of
            # readings -- never a real draw, just occasional detector noise.
            step = abs(rng.normal(0, 0.0008)) if rng.random() < 0.05 else 0.0
            value += step
            values.append(value)
        s = pd.Series(values, index=idx, dtype=float)

        result = compute_isolation_forest_anomalies(s, contamination=0.02)
        assert not result.empty
        assert result['is_anomaly'].sum() == 0

    def test_missing_sklearn_returns_empty(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "sklearn.ensemble" or name.startswith("sklearn"):
                raise ImportError("simulated missing sklearn")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        s = make_household_series(n_days=14)
        result = compute_isolation_forest_anomalies(s)
        assert result.empty

    def test_only_evaluates_most_recent_24h(self):
        s = make_household_series(n_days=10)
        result = compute_isolation_forest_anomalies(s)
        span = result.index.max() - result.index.min()
        assert span <= pd.Timedelta(hours=24)
