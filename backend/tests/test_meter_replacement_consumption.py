"""
Tests for splitting consumption aggregations (getConPerYear/Month/Day) at
confirmed meter replacements, so a period spanning a physical meter swap
doesn't report a large negative "consumption" for the artificial downward
jump.
"""
from datetime import datetime

import pytest


def seed_readings(db, readings):
    """Insert a list of (datetime, filtered_value) tuples as Reading rows."""
    for i, (time, value) in enumerate(readings):
        db.Reading.create(
            time=time,
            totalconsumption=value,
            filtered=value,
            imageName=f"dummy_{i}.jpg",
        )


class TestConPerYearAcrossReplacement:
    def test_replacement_within_year_does_not_go_negative(self, db_module):
        year = 2026
        seed_readings(db_module, [
            (datetime(year, 1, 1, 0, 0), 100.0),
            (datetime(year, 6, 1, 0, 0), 435.2),   # last reading of old meter
            (datetime(year, 6, 1, 0, 10), 0.3),     # first reading of new meter
            (datetime(year, 12, 31, 23, 50), 5.0),  # last reading of the year
        ])
        db_module.addMeterReplacement(datetime(year, 6, 1, 0, 5))

        result = db_module.getConPerYear()

        assert len(result) == 1
        expected = (435.2 - 100.0) + (5.0 - 0.3)
        assert result[0]["consumption"] == pytest.approx(expected)
        # first/last stay the raw meter readings so the UI can still show them.
        assert result[0]["first"] == 100.0
        assert result[0]["last"] == 5.0

    def test_year_without_replacement_is_unaffected(self, db_module):
        year = 2026
        seed_readings(db_module, [
            (datetime(year, 1, 1, 0, 0), 100.0),
            (datetime(year, 12, 31, 23, 50), 150.0),
        ])
        result = db_module.getConPerYear()
        assert result[0]["consumption"] == pytest.approx(50.0)

    def test_replacement_outside_year_does_not_affect_it(self, db_module):
        # Replacement confirmed a year before this period started must not
        # be applied a second time to a period that never straddled it.
        seed_readings(db_module, [
            (datetime(2025, 1, 1, 0, 0), 10.0),
            (datetime(2025, 12, 31, 0, 0), 20.0),
            (datetime(2026, 1, 1, 0, 0), 0.0),
            (datetime(2026, 12, 31, 0, 0), 30.0),
        ])
        db_module.addMeterReplacement(datetime(2025, 6, 1, 0, 0))

        result = db_module.getConPerYear()
        byYear = {r["year"]: r for r in result}
        assert byYear["2026"]["consumption"] == pytest.approx(30.0)


class TestConPerMonthAcrossReplacement:
    def test_replacement_within_month_does_not_go_negative(self, db_module):
        seed_readings(db_module, [
            (datetime(2026, 3, 1, 0, 0), 200.0),
            (datetime(2026, 3, 15, 0, 0), 250.0),   # last reading of old meter
            (datetime(2026, 3, 15, 0, 10), 0.1),     # first reading of new meter
            (datetime(2026, 3, 31, 0, 0), 2.0),
        ])
        db_module.addMeterReplacement(datetime(2026, 3, 15, 0, 5))

        result = db_module.getConPerMonth(2026)
        march = next(r for r in result if r["year-month"] == "2026-03")

        expected = (250.0 - 200.0) + (2.0 - 0.1)
        assert march["consumption"] == pytest.approx(expected)


class TestConPerDayAcrossReplacement:
    def test_replacement_within_day_does_not_go_negative(self, db_module):
        seed_readings(db_module, [
            (datetime(2026, 4, 10, 6, 0), 50.0),
            (datetime(2026, 4, 10, 12, 0), 55.0),   # last reading of old meter
            (datetime(2026, 4, 10, 12, 10), 0.05),   # first reading of new meter
            (datetime(2026, 4, 10, 18, 0), 1.0),
        ])
        db_module.addMeterReplacement(datetime(2026, 4, 10, 12, 5))

        result = db_module.getConPerDay(2026, 4)
        day10 = next(r for r in result if r["day"] == 10)

        expected = (55.0 - 50.0) + (1.0 - 0.05)
        assert day10["consumption"] == pytest.approx(expected)


class TestConsumptionBetweenAcrossReplacement:
    """
    getConsumptionBetween backs /api/v1/consumptionbetween, used by the
    frontend's "this week" card instead of computing last-minus-first
    client-side from /api/v1/readings (which doesn't know about meter
    replacements -- see CardDataStatsWeek.jsx).
    """

    def test_replacement_within_range_does_not_go_negative(self, db_module):
        seed_readings(db_module, [
            (datetime(2026, 5, 1, 0, 0), 100.0),
            (datetime(2026, 5, 3, 0, 0), 120.0),    # last reading of old meter
            (datetime(2026, 5, 3, 0, 10), 0.2),      # first reading of new meter
            (datetime(2026, 5, 7, 0, 0), 3.0),
        ])
        db_module.addMeterReplacement(datetime(2026, 5, 3, 0, 5))

        result = db_module.getConsumptionBetween(
            datetime(2026, 5, 1, 0, 0), datetime(2026, 5, 7, 23, 59)
        )

        expected = (120.0 - 100.0) + (3.0 - 0.2)
        assert result["consumption"] == pytest.approx(expected)
        assert result["first"] == 100.0
        assert result["last"] == 3.0

    def test_range_without_replacement_is_unaffected(self, db_module):
        seed_readings(db_module, [
            (datetime(2026, 5, 1, 0, 0), 100.0),
            (datetime(2026, 5, 7, 0, 0), 130.0),
        ])
        result = db_module.getConsumptionBetween(
            datetime(2026, 5, 1, 0, 0), datetime(2026, 5, 7, 23, 59)
        )
        assert result["consumption"] == pytest.approx(30.0)

    def test_empty_range_returns_none(self, db_module):
        result = db_module.getConsumptionBetween(
            datetime(2026, 5, 1, 0, 0), datetime(2026, 5, 7, 23, 59)
        )
        assert result == {"first": None, "last": None, "consumption": None}

    def test_replacement_outside_range_does_not_affect_it(self, db_module):
        seed_readings(db_module, [
            (datetime(2026, 5, 1, 0, 0), 10.0),
            (datetime(2026, 5, 7, 0, 0), 40.0),
        ])
        db_module.addMeterReplacement(datetime(2026, 4, 1, 0, 0))

        result = db_module.getConsumptionBetween(
            datetime(2026, 5, 1, 0, 0), datetime(2026, 5, 7, 23, 59)
        )
        assert result["consumption"] == pytest.approx(30.0)


class TestCumulativeTotal:
    """
    getCumulativeTotal backs the "cumtoday"/"cumtotal" dashboard card
    (CardDataStatsCum.jsx): after a confirmed replacement, the raw last
    reading is only the new meter's own count, not the overall total.
    """

    def test_no_replacement_returns_last_reading(self, db_module):
        seed_readings(db_module, [
            (datetime(2026, 1, 1, 0, 0), 100.0),
            (datetime(2026, 1, 2, 0, 0), 105.0),
        ])
        assert db_module.getCumulativeTotal() == pytest.approx(105.0)

    def test_replacement_adds_back_old_meters_last_reading(self, db_module):
        seed_readings(db_module, [
            (datetime(2026, 1, 1, 0, 0), 100.0),
            (datetime(2026, 6, 1, 0, 0), 435.2),   # last reading of old meter
            (datetime(2026, 6, 1, 0, 10), 0.3),     # first reading of new meter
            (datetime(2026, 6, 5, 0, 0), 2.0),      # latest reading, new meter
        ])
        db_module.addMeterReplacement(datetime(2026, 6, 1, 0, 5))

        expected = 2.0 + 435.2
        assert db_module.getCumulativeTotal() == pytest.approx(expected)

    def test_two_replacements_both_added_back(self, db_module):
        seed_readings(db_module, [
            (datetime(2026, 1, 1, 0, 0), 100.0),
            (datetime(2026, 4, 1, 0, 0), 200.0),    # last of meter 1
            (datetime(2026, 4, 1, 0, 10), 0.0),      # first of meter 2
            (datetime(2026, 8, 1, 0, 0), 50.0),      # last of meter 2
            (datetime(2026, 8, 1, 0, 10), 0.0),      # first of meter 3
            (datetime(2026, 12, 31, 0, 0), 20.0),
        ])
        db_module.addMeterReplacement(datetime(2026, 4, 1, 0, 5))
        db_module.addMeterReplacement(datetime(2026, 8, 1, 0, 5))

        expected = 20.0 + (200.0 - 0.0) + (50.0 - 0.0)
        assert db_module.getCumulativeTotal() == pytest.approx(expected)

    def test_no_readings_returns_none(self, db_module):
        assert db_module.getCumulativeTotal() is None


class TestMultipleReplacements:
    def test_two_replacements_in_same_year_both_corrected(self, db_module):
        year = 2026
        seed_readings(db_module, [
            (datetime(year, 1, 1, 0, 0), 100.0),
            (datetime(year, 4, 1, 0, 0), 200.0),    # last of meter 1
            (datetime(year, 4, 1, 0, 10), 0.0),      # first of meter 2
            (datetime(year, 8, 1, 0, 0), 50.0),      # last of meter 2
            (datetime(year, 8, 1, 0, 10), 0.0),      # first of meter 3
            (datetime(year, 12, 31, 0, 0), 20.0),
        ])
        db_module.addMeterReplacement(datetime(year, 4, 1, 0, 5))
        db_module.addMeterReplacement(datetime(year, 8, 1, 0, 5))

        result = db_module.getConPerYear()
        expected = (200.0 - 100.0) + (50.0 - 0.0) + (20.0 - 0.0)
        assert result[0]["consumption"] == pytest.approx(expected)
