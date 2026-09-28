"""
Tests for the /api/v1/meterreplacement/confirm endpoint in restapi.py,
called directly as a plain Python function per the restapi_module fixture
in conftest.py (see test_restapi_readings.py for the same pattern).
"""
from datetime import datetime, timedelta

import pytest


def test_confirm_meter_replacement_records_replacement(restapi_module, db_module):
    result = restapi_module.confirmMeterReplacementApi()

    assert result == {"ok": True}
    replacements = list(db_module.MeterReplacement.select())
    assert len(replacements) == 1


def test_confirm_meter_replacement_makes_negative_delta_detector_accept_new_reading(
    restapi_module, db_module
):
    import outlierDetection as od

    now = datetime.now()
    db_module.Reading.create(
        time=now - timedelta(minutes=10),
        totalconsumption=435.2,
        filtered=435.2,
        imageName="old.jpg",
    )

    # Before confirmation, the old high reading wins.
    assert od.negativeDeltaDetector(0.5) == 435.2

    restapi_module.confirmMeterReplacementApi()

    db_module.Reading.create(
        time=datetime.now(),
        totalconsumption=0.5,
        filtered=0.5,
        imageName="new.jpg",
    )
    assert od.negativeDeltaDetector(0.55) == 0.55


def test_confirm_meter_replacement_clears_pending_observation(restapi_module):
    import outlierDetection as od

    od.setKeyValueStoreValue(od.METERREPLACEMENT_PENDING_KEY, "0.5,0.6")
    restapi_module.confirmMeterReplacementApi()
    assert od.getKeyValueStoreValue(od.METERREPLACEMENT_PENDING_KEY, default="") == ""


def test_consumption_between_endpoint_corrects_for_replacement(restapi_module, db_module):
    """
    Regression test for the "this week" card reporting a large negative
    consumption after a confirmed meter replacement: it must go through
    /api/v1/consumptionbetween (backed by db.getConsumptionBetween), not
    compute last-minus-first client-side.
    """
    db_module.Reading.create(
        time=datetime(2026, 5, 1, 0, 0),
        totalconsumption=100.0,
        filtered=100.0,
        imageName="a.jpg",
    )
    db_module.Reading.create(
        time=datetime(2026, 5, 3, 0, 0),
        totalconsumption=120.0,
        filtered=120.0,
        imageName="b.jpg",
    )
    db_module.addMeterReplacement(datetime(2026, 5, 3, 0, 5))
    db_module.Reading.create(
        time=datetime(2026, 5, 3, 0, 10),
        totalconsumption=0.2,
        filtered=0.2,
        imageName="c.jpg",
    )
    db_module.Reading.create(
        time=datetime(2026, 5, 7, 0, 0),
        totalconsumption=3.0,
        filtered=3.0,
        imageName="d.jpg",
    )

    result = restapi_module.consumptionBetween(
        start="2026-05-01T00:00:00", end="2026-05-07T23:59:00"
    )

    expected = (120.0 - 100.0) + (3.0 - 0.2)
    assert result.consumption == pytest.approx(expected)
    assert result.consumption > 0


def test_consumption_between_endpoint_empty_range(restapi_module, db_module):
    result = restapi_module.consumptionBetween(
        start="2026-05-01T00:00:00", end="2026-05-07T23:59:00"
    )
    assert result.first is None
    assert result.last is None
    assert result.consumption is None


def test_last_reading_endpoint_reports_cumulative_total_after_replacement(
    restapi_module, db_module
):
    """
    Regression test for the dashboard's "total consumption" card showing the
    new meter's own low reading instead of the actual overall total right
    after a confirmed replacement -- see CardDataStatsCum.jsx.
    """
    db_module.Reading.create(
        time=datetime(2026, 6, 1, 0, 0),
        totalconsumption=435.2,
        filtered=435.2,
        imageName="old.jpg",
    )
    db_module.addMeterReplacement(datetime(2026, 6, 1, 0, 5))
    db_module.Reading.create(
        time=datetime(2026, 6, 1, 0, 10),
        totalconsumption=0.3,
        filtered=0.3,
        imageName="new.jpg",
    )

    result = restapi_module.getLastReading()

    assert result.totalconsumption == pytest.approx(0.3)
    assert result.cumulativeTotal == pytest.approx(0.3 + 435.2)


def test_meter_replacements_endpoint_lists_confirmed_replacements(
    restapi_module, db_module
):
    db_module.addMeterReplacement(datetime(2026, 4, 1, 0, 5))
    db_module.addMeterReplacement(datetime(2026, 8, 1, 0, 5))

    result = restapi_module.getMeterReplacementsApi()

    assert [r.time for r in result] == [
        datetime(2026, 4, 1, 0, 5),
        datetime(2026, 8, 1, 0, 5),
    ]
