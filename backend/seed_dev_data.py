"""
Local dev-only helper: fills the local SQLite DB with simulated readings so
the frontend charts and the leakage detectors (dual-check, Z-score,
Isolation Forest) have something meaningful to show.

Not used in production / not referenced by restapi.py or any cron job --
run manually:

    python backend/seed_dev_data.py

Safe to re-run: it wipes existing Reading/Notification rows first.
"""
import random
from datetime import datetime, timedelta

from db import db, Reading, Notification, KeyValueStore, create_db_if_not_exists

SLOT_MINUTES = 15  # must match leakageDetector.SLOT_MINUTES / the real cron interval
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
DAYS_OF_HISTORY = 32  # a bit more than leakageDetector.BASELINE_LOOKBACK_DAYS (30)


def simulate_readings(days: int, start_value: float = 100.0):
    """
    Build a synthetic 'filtered' cumulative meter series, oldest first.

    - Baseline: near-zero flow with small YOLO-style read jitter (what
      leakageDetector.estimate_noise_threshold is meant to tolerate).
    - Two daily consumption bursts (morning/evening), like showers/laundry.
    - A slow leak injected into the final 24h window so the detectors
      (dual-check + adaptive models) have something real to flag.
    """
    random.seed(42)
    now = datetime.now()
    total_slots = days * SLOTS_PER_DAY
    start_time = now - timedelta(minutes=SLOT_MINUTES * total_slots)

    value = start_value
    rows = []
    leak_start_slot = total_slots - SLOTS_PER_DAY  # last 24h

    for slot in range(total_slots):
        t = start_time + timedelta(minutes=SLOT_MINUTES * slot)
        hour = t.hour

        flow = 0.0
        if hour in (7, 8, 19, 20) and random.random() < 0.35:
            # a shower/laundry-sized burst, in m^3 per SLOT_MINUTES slot
            flow = random.uniform(0.008, 0.03)
        elif random.random() < 0.05:
            # occasional small draw (tap use)
            flow = random.uniform(0.001, 0.004)

        # constant slow leak during the final simulated day, on top of
        # normal usage -- ~3 L / 15min = ~12 L/h
        if slot >= leak_start_slot:
            flow += 0.003

        # detection-algorithm jitter: the true flow above is exact, but the
        # YOLO-read cumulative value wobbles by a small amount either way
        jitter = random.uniform(-0.0006, 0.0006)

        value += flow
        reading_value = value + jitter
        rows.append((t, reading_value))

    return rows


def seed():
    create_db_if_not_exists()
    db.connect(reuse_if_open=True)

    print("Clearing existing Reading/Notification rows...")
    Reading.delete().execute()
    Notification.delete().execute()
    KeyValueStore.delete().where(KeyValueStore.key == "leakDebCounter").execute()

    print(f"Simulating {DAYS_OF_HISTORY} days of readings ({SLOTS_PER_DAY} slots/day)...")
    rows = simulate_readings(DAYS_OF_HISTORY)

    print(f"Inserting {len(rows)} readings...")
    with db.atomic():
        batch = []
        for t, value in rows:
            batch.append({
                "time": t,
                "totalconsumption": value,
                "filtered": value,
                "imageName": "seed.jpg",
            })
            if len(batch) >= 500:
                Reading.insert_many(batch).execute()
                batch = []
        if batch:
            Reading.insert_many(batch).execute()

    Notification.create(
        time=datetime.now() - timedelta(hours=2),
        message="Risk of leakage as no durations with zero flow detected in the last 24 hours.",
        i18nIdentifier="leakdetected",
        type="warning",
        viewed=False,
    )

    db.close()
    print("Done. Seeded DB with a simulated slow leak in the final 24h window.")


if __name__ == "__main__":
    seed()
