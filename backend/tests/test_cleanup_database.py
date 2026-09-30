"""
Tests for the nightly cleanup (db.thin_out_old_images) and the index-based
consumption aggregations that rely on readings being kept at full
resolution:

- Readings are never deleted; only photos of days older than
  FULL_RESOLUTION_IMAGE_RETENTION_DAYS are thinned out to the first/last
  reading of each day.
- getConPerYear/Month/Day must still pick the right first/last reading per
  period, including across years without any readings.
"""
import os
from datetime import datetime, timedelta

import pytest


def _photo(images_dir, name, bbox=False):
    for filename in [name] + ([name + "_bbox.jpg"] if bbox else []):
        with open(os.path.join(images_dir, filename), "w"):
            pass


def _reading(db, time, value, imageName):
    db.Reading.create(time=time, totalconsumption=value, filtered=value, imageName=imageName)


class TestThinOutOldImages:
    def test_old_day_keeps_only_first_and_last_photo_but_all_readings(self, db_module, images_dir):
        oldDay = (datetime.now() - timedelta(days=db_module.FULL_RESOLUTION_IMAGE_RETENTION_DAYS + 5)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        names = []
        for i in range(4):
            name = f"old_{i}.jpg"
            names.append(name)
            _photo(images_dir, name, bbox=True)
            _reading(db_module, oldDay + timedelta(hours=6 * i), float(i), name)

        db_module.thin_out_old_images()

        assert db_module.Reading.select().count() == 4
        remaining = set(os.listdir(images_dir))
        assert remaining == {
            "old_0.jpg", "old_0.jpg_bbox.jpg",
            "old_3.jpg", "old_3.jpg_bbox.jpg",
        }

    def test_recent_days_keep_every_photo(self, db_module, images_dir):
        now = datetime.now()
        for i in range(3):
            name = f"recent_{i}.jpg"
            _photo(images_dir, name)
            _reading(db_module, now - timedelta(minutes=15 * (i + 1)), float(i), name)

        db_module.thin_out_old_images()

        assert set(os.listdir(images_dir)) == {"recent_0.jpg", "recent_1.jpg", "recent_2.jpg"}

    def test_is_idempotent_when_photos_are_already_gone(self, db_module, images_dir):
        oldDay = datetime.now() - timedelta(days=db_module.FULL_RESOLUTION_IMAGE_RETENTION_DAYS + 5)
        for i in range(3):
            _reading(db_module, oldDay.replace(hour=i + 1), float(i), f"gone_{i}.jpg")

        db_module.thin_out_old_images()
        db_module.thin_out_old_images()

        assert db_module.Reading.select().count() == 3


class TestConPerYearIndexed:
    def test_multiple_years_with_gap_year(self, db_module):
        _reading(db_module, datetime(2023, 1, 1, 0, 0), 10.0, "a.jpg")
        _reading(db_module, datetime(2023, 12, 31, 23, 45), 60.0, "b.jpg")
        # 2024 has no readings at all -> no entry for it
        _reading(db_module, datetime(2025, 1, 1, 0, 0), 100.0, "c.jpg")
        _reading(db_module, datetime(2025, 6, 1, 0, 0), 130.0, "d.jpg")
        _reading(db_module, datetime(2025, 12, 31, 23, 45), 200.0, "e.jpg")

        result = db_module.getConPerYear()

        assert [r["year"] for r in result] == ["2023", "2025"]
        assert result[0]["consumption"] == pytest.approx(50.0)
        assert result[1]["first"] == 100.0
        assert result[1]["last"] == 200.0
        assert result[1]["consumption"] == pytest.approx(100.0)
        assert result[1]["rate"] == pytest.approx(100.0)

    def test_failed_detections_are_ignored(self, db_module):
        db_module.Reading.create(time=datetime(2026, 1, 1, 0, 0), totalconsumption=None, filtered=None, imageName="f.jpg")
        _reading(db_module, datetime(2026, 1, 2, 0, 0), 5.0, "a.jpg")
        _reading(db_module, datetime(2026, 3, 1, 0, 0), 8.0, "b.jpg")
        db_module.Reading.create(time=datetime(2026, 3, 2, 0, 0), totalconsumption=None, filtered=None, imageName="g.jpg")

        result = db_module.getConPerYear()

        assert len(result) == 1
        assert result[0]["consumption"] == pytest.approx(3.0)

    def test_empty_database(self, db_module):
        assert db_module.getConPerYear() == []


def test_month_and_day_aggregations_respect_period_boundaries(db_module):
    # Readings right before/after the period must not leak into it now that
    # the filters are time ranges instead of strftime()-based year/month.
    _reading(db_module, datetime(2025, 12, 31, 23, 45), 1.0, "a.jpg")
    _reading(db_module, datetime(2026, 1, 1, 0, 0), 2.0, "b.jpg")
    _reading(db_module, datetime(2026, 1, 1, 23, 45), 4.0, "c.jpg")
    _reading(db_module, datetime(2026, 1, 31, 23, 45), 5.0, "d.jpg")
    _reading(db_module, datetime(2026, 2, 1, 0, 0), 9.0, "e.jpg")

    # getConPerMonth pads the year to 12 months (consumption=None when empty).
    months = [m for m in db_module.getConPerMonth(2026) if m["consumption"] is not None]
    assert [(m["year-month"], m["consumption"]) for m in months] == [
        ("2026-01", pytest.approx(3.0)),
        ("2026-02", pytest.approx(0.0)),
    ]

    days = [d for d in db_module.getConPerDay(2026, 1) if d["consumption"] is not None]
    assert [(d["day"], d["consumption"]) for d in days] == [
        (1, pytest.approx(2.0)),
        (31, pytest.approx(0.0)),
    ]

    # December uses the year rollover in _startOfNextMonth.
    decemberDays = [d for d in db_module.getConPerDay(2025, 12) if d["consumption"] is not None]
    assert [d["day"] for d in decemberDays] == [31]
