import pandas as pd
from db import get_last_readings, get_readings_since, addNotification, getKeyValueStoreValue, setKeyValueStoreValue
from emailhandler import sendEmail
from mqtt import mqtt_publish
from mylog import Logger

logger = Logger("leakageDetector")

# How many days of history to pull for the adaptive baseline models
# (Z-score, Isolation Forest). Readings are kept at full resolution forever
# (only old photos are thinned out, see db.py:thin_out_old_images), so this
# can be raised -- at the cost of an older, possibly less representative
# baseline (seasonal usage) and longer Isolation Forest training on the Pi.
BASELINE_LOOKBACK_DAYS = 30

# Readings are taken every 15 minutes -> 96 slots per day. Must match the
# cron interval set in restapi.py (job_readmeasurement.minute.every(...)) --
# if that changes, this needs to change with it, or the per-slot baseline
# statistics below silently compare readings against the wrong time-of-day
# bucket.
SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES

# Lower bound for the adaptive threshold, in m^3 (same unit as the
# 'filtered' reading, see readTotalConsumption.py:gettotalconsumption).
# Keeps the detector from becoming overly sensitive when the recent window
# happens to be unusually clean (e.g. right after startup), since MAD would
# otherwise estimate near-zero noise. Set to 0.002 m^3 (2 liters) as a
# conservative estimate of the YOLO reading's jitter -- NOT 1.0, which
# would mean 1000 liters of slack per 24h window and mask real leaks.
MIN_NOISE_THRESHOLD = 0.002

# A gap between two consecutive readings wider than this is treated as a
# measurement outage (camera down, Pi offline, ...) rather than normal
# sampling jitter around the SLOT_MINUTES interval. Used to keep long outages
# (hours to months) from being fed into the models as regular data -- see
# split_off_last_gap.
GAP_THRESHOLD_HOURS = 6.0

# Minimum number of historical observations a time-of-day slot needs before
# compute_zscore_anomalies trusts its mean/std enough to flag anything in
# it. Any spread estimate (std or MAD) computed from just 2-5 points is
# mostly sampling noise, not a real measurement of "how much this slot
# normally varies" -- a fresh install (or one that just lost history to a
# gap) has EVERY slot starting from 0 observations and climbing by ~1 per
# day, so this directly controls how many days a slot needs to "warm up"
# before the model will flag it (BASELINE_LOOKBACK_DAYS=30 is the ceiling
# on history it will ever consider, not a guarantee that a slot has that
# many observations yet). 10 was chosen over the previous 3 after a
# production incident where 3-5 observations per slot (site was ~1 week
# old) produced triple-digit z-scores on ordinary jitter.
#
# DELIBERATE TRADE-OFF, not just a side effect: during the ~10-day warm-up
# this makes the Z-score model blind to real leaks too, not only false
# positives (verified: a realistic 12 L/h leak injected into the real,
# ~7-day-old production baseline was NOT flagged by this model with any
# min_std, because every slot's count fell short of this threshold before
# min_std ever entered the calculation). That's accepted rather than worked
# around (e.g. by relaxing the threshold or special-casing all-zero slot
# histories) because find_stable_periods_dual_check and Isolation Forest's
# own min_training_rows are this detector's other two signals, don't
# depend on any single slot's history, and both DID catch that same
# injected leak -- detectLeakage()'s overall sensitivity during warm-up
# rests on them, not on this one. Once a slot has genuinely earned this
# many observations, MAD-based std (see _mad_std) does correctly separate
# ordinary jitter from a real leak's excess volume.
ZSCORE_MIN_SLOT_OBSERVATIONS = 10


def estimate_noise_threshold(time_series: pd.Series, k: float = 6.0, min_threshold: float = MIN_NOISE_THRESHOLD) -> float:
    """
    Estimate a noise threshold for consecutive-step comparisons from the
    time series itself, so isolated detection-algorithm jitter (e.g. YOLO
    misreads causing +/- a few units around a true-zero flow) doesn't get
    mistaken for real consumption.

    Uses the Median Absolute Deviation (MAD) of consecutive differences,
    scaled to be comparable to a standard deviation, times a safety factor k.
    MAD is used instead of std() because it isn't dragged around by the
    (rare, but expected) large genuine-consumption steps mixed into the
    same window.

    Since the cumulative meter reading never decreases (upstream outlier
    filtering already drops negative deltas) and most SLOT_MINUTES intervals
    in a real household see zero consumption, the raw diff series is
    dominated by exact zeros. Including those would pull the MAD down to
    ~0 regardless of how noisy the actual (non-zero) readings are, so the
    noise scale is estimated from the positive diffs only -- these are the
    ones that actually carry the detection algorithm's read-to-read jitter.

    Parameters:
    time_series (pd.Series): the readings to estimate noise from.
    k (float): safety multiplier applied to the estimated noise scale.
    min_threshold (float): floor for the returned threshold.

    Returns:
    float: threshold to use for `find_stable_periods_dual_check`.
    """
    diffs = time_series.diff().dropna()
    positive_diffs = diffs[diffs > 0]
    if positive_diffs.empty:
        return min_threshold

    median_diff = positive_diffs.median()
    mad = (positive_diffs - median_diff).abs().median()
    # Consistency constant to make MAD comparable to a standard deviation
    # under normally distributed noise.
    noise_std_estimate = mad * 1.4826

    threshold = k * noise_std_estimate
    return max(threshold, min_threshold)

def split_off_last_gap(time_series: pd.Series, gap_threshold_hours: float = GAP_THRESHOLD_HOURS) -> pd.Series:
    """
    Drop everything up to and including the last measurement outage
    (a gap between consecutive readings wider than gap_threshold_hours),
    so callers only see the data from the point measurements resumed.

    Without this, a long outage (camera failure, Pi offline, a many-month
    pause, ...) shows up as a single reading-to-reading diff equal to the
    *entire* consumption accumulated during the outage. Fed into flow
    features or a baseline model, that one point is a massive outlier and
    -- worse -- baseline models would keep treating the stale pre-outage
    history as valid "normal" data to compare fresh readings against.

    Resuming after a gap is treated the same as a fresh install: there
    simply isn't a trustworthy baseline yet, and the existing
    not-enough-data code paths (empty DataFrame / insufficient row counts)
    already degrade gracefully until enough post-gap history accumulates.

    Parameters:
    time_series (pd.Series): readings indexed by timestamp, ascending.
    gap_threshold_hours (float): a gap strictly wider than this counts as
        an outage.

    Returns:
    pd.Series: the tail of time_series starting at the first reading after
        the last qualifying gap, or the original series unchanged if it
        has no such gap.
    """
    if len(time_series) < 2:
        return time_series

    gaps = time_series.index.to_series().diff()
    is_gap = gaps > pd.Timedelta(hours=gap_threshold_hours)
    if not is_gap.any():
        return time_series

    last_gap_pos = is_gap.to_numpy().nonzero()[0][-1]
    return time_series.iloc[last_gap_pos:]


def get_last_readings_as_timeseries():
    """
    Retrieve the last SLOTS_PER_DAY readings (24h at SLOT_MINUTES sampling)
    from the database and transform them into a Pandas time series.

    Note: unlike get_baseline_readings_as_timeseries, this deliberately does
    NOT split off a trailing measurement gap. A gap here just becomes a
    large step in find_stable_periods_dual_check's diff, which correctly
    ends any stable-period candidate at that point without ever being
    mistaken for one (a stable period requires the diff to stay *below*
    the noise threshold) -- so no special-casing is needed for the dual
    check specifically.

    Parameters:
    db_connection (object): Database connection object
    table_name (str): Name of the table to retrieve readings from

    Returns:
    pd.Series: Pandas time series object containing the last SLOTS_PER_DAY
        readings.
    """
    # Retrieve the last 24h worth of readings from the database.
    readings = get_last_readings(SLOTS_PER_DAY)
    data = []
    readings = sorted(readings, key=lambda x: x.time)
    for r in readings:
        data.append((r.time, r.filtered))
    
    # Create a Pandas DataFrame from the readings. dtype=float is needed
    # for the empty-readings case (e.g. right after a fresh install): an
    # empty DataFrame column defaults to dtype=object, which fails the
    # numeric-dtype check in find_stable_periods_dual_check.
    df = pd.DataFrame(data, columns=['timestamp', 'value']).astype({'value': 'float64'})

    # Convert the 'timestamp' column to datetime format
    df['timestamp'] = pd.to_datetime(df['timestamp'])

    # Set the 'timestamp' column as the index of the DataFrame
    df.set_index('timestamp', inplace=True)

    # Convert the DataFrame to a Pandas time series
    ts = df['value']

    return ts


def get_baseline_readings_as_timeseries(days: int = BASELINE_LOOKBACK_DAYS) -> pd.Series:
    """
    Retrieve the last `days` days of readings from the database as a Pandas
    time series, for use as the training/baseline window of the adaptive
    models below. Unlike get_last_readings_as_timeseries (fixed count, used
    by the original dual-check detector), this is time-bounded so the
    baseline always covers full days regardless of how many readings were
    actually taken (e.g. gaps from camera failures).

    If the window contains a long measurement outage (see
    split_off_last_gap), everything up to and including that outage is
    dropped -- a baseline should not straddle a gap of unknown length
    (hours to months), since stale pre-outage readings are no longer a
    trustworthy "normal" to compare fresh ones against.

    Returns:
    pd.Series: cumulative meter readings ('filtered'), indexed by timestamp.
    """
    readings = get_readings_since(days)
    data = [(r.time, r.filtered) for r in readings if r.filtered is not None]
    # See get_last_readings_as_timeseries for why dtype=float64 is forced
    # here (empty-readings case would otherwise yield dtype=object).
    df = pd.DataFrame(data, columns=['timestamp', 'value']).astype({'value': 'float64'})
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    return split_off_last_gap(df['value'])


def compute_flow_features(time_series: pd.Series) -> pd.DataFrame:
    """
    Turn the raw cumulative meter reading into per-interval flow features
    used by both the Z-score and the Isolation Forest model.

    Columns:
    flow: consumption since the previous reading (>= 0; the meter is
        monotonic and negative deltas are already filtered upstream).
    active_flow_streak: number of consecutive readings (including this one)
        with flow > 0 -- mirrors 'dauer_aktiver_fluss' from the concept.
    rolling_sum_30m: sum of flow over the trailing 30 minutes of WALL-CLOCK
        time (a time-based rolling window, not a fixed count of readings)
        -- mirrors 'rollende_summe_30m'.
    hour_of_day, minute_of_day: time-of-day features. minute_of_day is
        rounded DOWN to the nearest SLOT_MINUTES bucket, not the exact
        minute -- see the minute_of_day note further below for why.

    A reading-to-reading gap wider than GAP_THRESHOLD_HOURS (measurement
    outage, e.g. camera down) has its flow value dropped rather than
    diffed: otherwise the entire consumption accumulated during the
    outage -- hours to months worth -- would show up as a single flow
    reading, an outlier disproportionate to actual per-interval flow.

    rolling_sum_30m deliberately uses a time-based window ('30min'), not a
    row-count window (e.g. round(30/SLOT_MINUTES) readings): readings are
    NOT reliably equidistant at SLOT_MINUTES apart -- a failed detection
    (bad lighting, a discarded outlier, a watchdog reboot mid-inference,
    ...) simply produces no row for that slot rather than a placeholder, so
    a fixed-count window can silently span hours of wall-clock time after a
    gap while still calling itself "30 minutes". That inflates
    rolling_sum_30m for the readings immediately following a gap (several
    real intervals' worth of flow lands in what the model believes is one
    30-minute window) and, symmetrically, corrupts the SAME slot's
    baseline statistics in compute_zscore_anomalies/compute_isolation_forest_anomalies
    whenever a gap happened to fall in the history window -- either can
    produce a spurious leak flag with the readings themselves looking
    unremarkable (observed in production: three nights of Z-score/Isolation
    Forest flags with every underlying reading at/near zero flow, traced
    back to this). A time-based window only ever sums flow that actually
    occurred within the trailing 30 minutes, regardless of how many (or
    how few) rows happen to fall in it.
    """
    df = pd.DataFrame({'value': time_series})
    df['flow'] = df['value'].diff().clip(lower=0)

    gaps = df.index.to_series().diff()
    df.loc[gaps > pd.Timedelta(hours=GAP_THRESHOLD_HOURS), 'flow'] = float('nan')

    is_flowing = df['flow'] > 0
    # Length of the current run of flowing readings, reset to 0 whenever
    # flow stops (or at the very first reading, where diff() is NaN).
    groups = (~is_flowing).cumsum()
    df['active_flow_streak'] = is_flowing.groupby(groups).cumsum()

    # Time-based window -- see the rolling_sum_30m docstring note above for
    # why this must not be a fixed row count. NaN flow rows (outages) are
    # skipped by sum() like any other NaN and don't poison the window; they
    # get dropped from the returned frame by dropna(subset=['flow']) below
    # anyway, since they were never a real per-interval flow reading.
    df['rolling_sum_30m'] = df['flow'].rolling('30min', min_periods=1).sum()

    df['hour_of_day'] = df.index.hour
    # Rounded DOWN to the nearest SLOT_MINUTES bucket, NOT the exact minute.
    # A reading nominally "every SLOT_MINUTES" still lands at a different
    # exact second/minute each time (camera-trigger jitter, and the
    # detection pipeline itself takes a variable few minutes -- see
    # YOLO_PREDICT_TIMEOUT_SECONDS in readTotalConsumption.py) -- e.g. three
    # consecutive ~06:04 readings landed at :04:11, :04:29, :04:36 exact
    # minute-of-day values 364 each time by coincidence, but the fourth
    # landed at :04:42 and would have missed that "slot" under exact-minute
    # grouping. Grouping by exact minute-of-day in compute_zscore_anomalies
    # therefore scatters what should be one coherent "around 06:00" sample
    # across many near-singleton groups, each with std=0 (or clipped to the
    # min_std floor) purely because they only ever saw 1-2 points --
    # trivially breached by any next reading whose flow differs even
    # slightly, producing spurious triple-digit z-scores on essentially
    # flat, unremarkable data (observed in production). Bucketing first
    # pools every reading "around" the same SLOT_MINUTES-wide window into
    # one group, giving slot_stats a realistic sample size to estimate mean/
    # std from.
    df['minute_of_day'] = (
        (df.index.hour * 60 + df.index.minute) // SLOT_MINUTES
    ) * SLOT_MINUTES

    return df.dropna(subset=['flow'])


def _mad_std(group: pd.Series) -> float:
    """
    Median Absolute Deviation of `group`, scaled to be comparable to a
    standard deviation (same consistency constant used by
    estimate_noise_threshold above). Returns NaN for a single-element
    group, matching pandas' std() convention (so the min_std floor in
    compute_zscore_anomalies applies uniformly regardless of which
    estimator produced the NaN).

    Used instead of pandas' std() for the per-slot baseline statistics in
    compute_zscore_anomalies: a night-time slot's history is typically a
    near-point-mass at flow=0 with rare small excursions (someone flushes
    a toilet once in 30 nights) rather than anything resembling a normal
    distribution. std() is quadratically sensitive to those rare
    excursions -- a single 0.05 m^3 night in an otherwise all-zero 30-day
    history inflates std enough to mostly hide it, while population
    without it collapses std to ~0 and turns the NEXT such excursion into
    a 100+ sigma event. The median of a mostly-zero sample is 0 regardless
    of a handful of excursions, so MAD measures the spread of the
    excursions themselves without either failure mode.
    """
    median = group.median()
    mad = (group - median).abs().median()
    return mad * 1.4826


def compute_zscore_anomalies(
    baseline: pd.Series,
    k: float = 3.0,
    min_std: float = 0.002,
    min_flagged_volume: float = MIN_NOISE_THRESHOLD,
) -> pd.DataFrame:
    """
    Adaptive Baseline + Z-Score model.

    For each SLOT_MINUTES time-of-day slot, builds a distribution of the
    rolling 30-minute consumption from all matching slots across the
    baseline window (i.e. "what does consumption around 03:15 normally
    look like, across the last N days?"), then scores the most recent
    day's readings against that distribution.

    Z(t) = (x(t) - mean(slot)) / std(slot)

    Scoring the rolling sum rather than the single-reading flow is
    deliberate: a slow drip (e.g. 3 L/h) is far too small to stand out
    against a single quiet reading's own noise, but accumulated over 30
    minutes it becomes a small, steady, and therefore detectable excess. A
    high positive Z at an hour that is normally quiet (e.g. night) is the
    signature of exactly that kind of slow leak; it also flags unusually
    high daytime flow, covering the sudden-large-usage case from a
    different angle than the Isolation Forest model.

    A plain Z-score is a poor fit for a mostly-quiet household, though:
    most night-time slots see EXACTLY zero flow, night after night -- the
    true distribution is a near-point-mass at 0 with rare small bumps, not
    anything close to normal. Two independent adjustments compensate for
    that (see the "in production" note on each for what this fixes):

    1. std is estimated via Median Absolute Deviation (_mad_std), not
       pandas' std(): std() is dominated by the rare bumps themselves (one
       0.05 m^3 night among 30 all-zero nights inflates std enough to hide
       a similarly-sized real leak, while a slot with NO bumps at all in
       its history collapses std towards 0 and turns the next ordinary
       bump into a triple-digit z-score). MAD is driven by the *typical*
       spread instead.
    2. A slot is only ever flagged if rolling_sum_30m also clears
       min_flagged_volume in absolute terms, regardless of how large its
       z-score is. Even MAD can't fully rescue a slot whose entire history
       is exactly 0 (MAD is then 0 too, floored to min_std) -- without this,
       a single 5 ml reading jitter there would still register as an
       "anomaly". A leak worth warning about is a physical volume, not a
       statistical artifact of an unusually clean slot.

    Parameters:
    baseline (pd.Series): cumulative readings covering the full lookback
        window (see get_baseline_readings_as_timeseries).
    k (float): Z-score threshold above which a slot is flagged anomalous.
    min_std (float): floor for a slot's MAD-based std estimate, so slots
        that happen to be perfectly quiet in the baseline (MAD=0) don't
        produce an infinite/undefined Z-score for the first non-zero
        reading. Matches MIN_NOISE_THRESHOLD (the same "smallest
        physically meaningful reading jitter" floor used by
        estimate_noise_threshold), not an arbitrary numerical-stability
        epsilon -- see point 1 above for why a tiny floor defeats the
        purpose.
    min_flagged_volume (float): a slot's rolling_sum_30m must reach at
        least this many m^3 to be eligible for flagging at all, no matter
        its z-score -- see point 2 above.

    Returns:
    pd.DataFrame: the most recent day's flow readings with a 'zscore' and
        'is_anomaly' column. Empty if there isn't enough baseline history
        to compute per-slot statistics.
    """
    features = compute_flow_features(baseline)
    if features.empty:
        return features

    # Evaluate the most recent day of readings against statistics built
    # from everything *before* it. Including the evaluated day in its own
    # baseline would let a leak inflate the very mean/std it's being
    # compared against, hiding itself (verified empirically: a slow night
    # leak survived undetected until this was excluded).
    latest_day_start = features.index.max() - pd.Timedelta(hours=24)
    recent = features[features.index > latest_day_start].copy()
    history = features[features.index <= latest_day_start]

    # See ZSCORE_MIN_SLOT_OBSERVATIONS for why this must not be too low --
    # a slot's mean/std is only trusted once it has enough history behind
    # it.
    slot_stats = history.groupby('minute_of_day')['rolling_sum_30m'].agg(
        mean='mean', std=_mad_std, count='count'
    )
    slot_stats['std'] = slot_stats['std'].fillna(0).clip(lower=min_std)

    recent = recent.join(slot_stats, on='minute_of_day', rsuffix='_slot')
    recent = recent[recent['count'] >= ZSCORE_MIN_SLOT_OBSERVATIONS]
    if recent.empty:
        return recent

    recent['zscore'] = (recent['rolling_sum_30m'] - recent['mean']) / recent['std']
    recent['is_anomaly'] = (recent['zscore'] > k) & (recent['rolling_sum_30m'] >= min_flagged_volume)

    return recent


def compute_isolation_forest_anomalies(
    baseline: pd.Series,
    contamination: float = 0.02,
    min_flagged_volume: float = MIN_NOISE_THRESHOLD,
):
    """
    Isolation Forest model (unsupervised ML).

    Trains on the engineered flow features (flow, active_flow_streak,
    rolling_sum_30m, hour_of_day) across the full baseline window, then
    scores the most recent day. Isolation Forest isolates anomalies by how
    few random splits it takes to separate a point from the rest -- points
    that combine an unusual flow rate with an unusual time-of-day (e.g.
    steady flow at 3am, or a very high single-reading flow at any hour)
    isolate faster than typical points and get flagged.

    Unlike the Z-score model, this captures *combinations* of features
    (e.g. "10 L in one interval is normal at 08:00 but not at 03:00")
    rather than scoring each slot independently, and it also directly
    incorporates streak length and rolling sum, which the Z-score model
    (scored per single reading) does not.

    Requires scikit-learn (see setupscript.sh). Returns an empty DataFrame
    if scikit-learn isn't installed or there isn't enough baseline data,
    so callers can treat this as an optional signal.

    Parameters:
    baseline (pd.Series): cumulative readings covering the full lookback window.
    contamination (float): expected fraction of anomalous points in the
        training data, passed through to IsolationForest. This is a
        proportion, not a physical threshold -- with contamination=0.02,
        the model WILL flag roughly 2% of points as anomalous even when
        every single one of them is physically meaningless (e.g. a night of
        exact-zero flow with one 1.4 ml jitter reading, observed in
        practice): something always looks the most unusual relative to the
        rest, even in an entirely leak-free household. min_flagged_volume
        below is what keeps that statistical artifact from ever being
        reported as a leak signal.
    min_flagged_volume (float): a reading's rolling_sum_30m must reach at
        least this many m^3 to be eligible for flagging at all, regardless
        of its anomaly_score -- same rationale and same default
        (MIN_NOISE_THRESHOLD) as compute_zscore_anomalies's parameter of
        the same name.

    Returns:
    pd.DataFrame: the most recent day's readings with an 'anomaly_score'
        column (lower = more anomalous, matches sklearn's decision_function)
        and an 'is_anomaly' boolean column.
    """
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError:
        logger.logger.warning("scikit-learn not installed; skipping Isolation Forest leakage check")
        return pd.DataFrame()

    features = compute_flow_features(baseline)
    if features.empty:
        return features

    # IsolationForest needs a reasonable amount of data to learn a sensible
    # notion of "normal"; below this it would mostly be reacting to noise.
    min_training_rows = 3 * SLOTS_PER_DAY
    feature_cols = ['flow', 'active_flow_streak', 'rolling_sum_30m', 'hour_of_day']

    # Train on everything *before* the evaluated day only. Training on the
    # full window (including the day being scored) would let a leak widen
    # the model's own notion of "normal" and mask itself -- the same
    # leakage-into-baseline issue as in compute_zscore_anomalies.
    latest_day_start = features.index.max() - pd.Timedelta(hours=24)
    history = features[features.index <= latest_day_start]
    recent = features[features.index > latest_day_start].copy()
    if recent.empty:
        return recent
    if len(history) < min_training_rows:
        logger.logger.info(
            f"Not enough history before the evaluated day for Isolation Forest "
            f"({len(history)} rows, need >= {min_training_rows}); skipping"
        )
        return pd.DataFrame()

    model = IsolationForest(contamination=contamination, random_state=0)
    model.fit(history[feature_cols])

    recent['anomaly_score'] = model.decision_function(recent[feature_cols])
    recent['is_anomaly'] = (model.predict(recent[feature_cols]) == -1) & (
        recent['rolling_sum_30m'] >= min_flagged_volume
    )

    return recent


def find_stable_periods_dual_check(time_series: pd.Series, threshold: float=0, minDuration: float=1, debug=False) -> list[dict]:

    if not isinstance(time_series.index, pd.DatetimeIndex):
        raise TypeError("Input time_series must have a DatetimeIndex.")
    if not pd.api.types.is_numeric_dtype(time_series.dtype):
        raise TypeError("Time series values must be numeric.")

    results = []
    n = len(time_series)

    # A stable period needs at least two points (one 'step') to have a duration
    if n < 2:
        return results

    current_segment_start_idx = 0 # Assume the first point starts a potential segment

    # Iterate from the second point to evaluate the change from the previous point
    for i in range(1, n):
        # 1. Check for stability between consecutive points
        consecutive_change = abs(time_series.iloc[i] - time_series.iloc[i-1])

        if consecutive_change > threshold:
            # This point 'i' breaks the current sequence of stable consecutive steps.
            # Evaluate the segment that just ended (from current_segment_start_idx to i-1).
            segment_end_idx = i - 1

            # Ensure the segment has at least two points (i.e., spans at least one stable step)
            if segment_end_idx > current_segment_start_idx:
                # 2. Check for overall stability from the start to the end of this potential segment
                interval_change = abs(time_series.iloc[segment_end_idx] - time_series.iloc[current_segment_start_idx])

                if interval_change <= threshold:
                    # Both conditions are met: the segment is valid!
                    start_time = time_series.index[current_segment_start_idx]
                    end_time = time_series.index[segment_end_idx]
                    start_value = time_series.iloc[current_segment_start_idx]
                    end_value = time_series.iloc[segment_end_idx]

                    duration_td = end_time - start_time
                    duration_hours = duration_td.total_seconds() / 3600.0
                    total_change_in_interval = end_value - start_value # Store actual change (not absolute)

                    if duration_hours >= minDuration:
                        results.append({
                            'duration_hours': duration_hours,
                            'start_time': start_time.isoformat(),
                            'end_time': end_time.isoformat(),
                            'change_within_interval': total_change_in_interval
                        })

            # Reset the start of the next potential segment to the current point 'i'
            current_segment_start_idx = i

    # After the loop, check if there's an active stable segment extending to the end of the series
    segment_end_idx = n - 1 # The last point in the series

    if segment_end_idx > current_segment_start_idx: # Ensure it's a valid segment
        interval_change = abs(time_series.iloc[segment_end_idx] - time_series.iloc[current_segment_start_idx])

        if interval_change <= threshold:
            start_time = time_series.index[current_segment_start_idx]
            end_time = time_series.index[segment_end_idx]
            start_value = time_series.iloc[current_segment_start_idx]
            end_value = time_series.iloc[segment_end_idx]

            duration_td = end_time - start_time
            duration_hours = duration_td.total_seconds() / 3600.0
            total_change_in_interval = end_value - start_value

            if duration_hours >= minDuration:
                results.append({
                    'duration_hours': duration_hours,
                    'start_time': start_time.isoformat(),
                    'end_time': end_time.isoformat(),
                    'change_within_interval': total_change_in_interval
                })


    if debug:
        print("Stable Occurrences (Dual Check):")
        if results:
            for occ in results:
                print(f"- Start: {occ['start_time']}, End: {occ['end_time']}, "
                    f"Duration: {occ['duration_hours']:.4f} hours, "
                    f"Change: {occ['change_within_interval']:.4f}")
        else:
            print("No stable periods found meeting both criteria.")

    return results


def detectLeakage():
    logger.logger.info("Started detectLeakage")

    reasons = []

    # 1. Dual-check: is there any sufficiently long flow-free period in the
    # last 24h at all? Catches the "no rest period" signature of a leak
    # without needing any history beyond the current day.
    ts = get_last_readings_as_timeseries()
    threshold = estimate_noise_threshold(ts)
    logger.logger.info(f"Using adaptive noise threshold: {threshold:.4f}")
    stable_occurrences = find_stable_periods_dual_check(ts, threshold=threshold, debug=False)
    if len(stable_occurrences) == 0:
        reasons.append("no flow-free period found in the last 24 hours")

    # 2 & 3. Adaptive baseline models: is *today's* consumption pattern
    # unusual compared to the last BASELINE_LOOKBACK_DAYS days? These need
    # more history than the dual-check and degrade gracefully (empty
    # result) when that history isn't available yet (e.g. fresh install).
    baseline = get_baseline_readings_as_timeseries()

    zscore_result = compute_zscore_anomalies(baseline)
    zscore_anomalies = zscore_result[zscore_result['is_anomaly']] if not zscore_result.empty else zscore_result
    if not zscore_anomalies.empty:
        reasons.append(f"Z-score model flagged {len(zscore_anomalies)} anomalous reading(s)")

    iforest_result = compute_isolation_forest_anomalies(baseline)
    iforest_anomalies = iforest_result[iforest_result['is_anomaly']] if not iforest_result.empty else iforest_result
    if not iforest_anomalies.empty:
        reasons.append(f"Isolation Forest flagged {len(iforest_anomalies)} anomalous reading(s)")

    if reasons:
        for reason in reasons:
            logger.logger.info(f"Leakage signal: {reason}")
        leakDebCounter = getKeyValueStoreValue("leakDebCounter", default="0")
        leakDebCounter = int(leakDebCounter) + 1
        setKeyValueStoreValue("leakDebCounter", str(leakDebCounter))
        setKeyValueStoreValue("leakDebReasons", "; ".join(reasons))
    else:
        leakDebCounter = 0
        setKeyValueStoreValue("leakDebCounter", str(0))
    logger.logger.info(
        f"Finished detectLeakage: {len(reasons)} signal(s), "
        f"{leakDebCounter} consecutive day(s) flagged"
    )


def sendWarning():
    leakDebCounter = int(getKeyValueStoreValue("leakDebCounter", default="0"))
    if leakDebCounter > 2:
        logger.logger.info("3 Times a leakage detected. Sending Warning")
        subject = "WatermeterAI Leakage Detection"
        reasons = getKeyValueStoreValue("leakDebReasons", default="no durations with zero flow detected in the last 24 hours")
        message = f"Risk of leakage: {reasons}."
        sendEmail(subject, message)
        addNotification(message, "leakdetected", "warning")
        setKeyValueStoreValue("leakDebCounter", str(0))
        mqtt_publish("watermeter/notification/warning/leakage",message)





# --- Example Usage ---
if __name__ == "__main__":

    detectLeakage()
    sendWarning()
