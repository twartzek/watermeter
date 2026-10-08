"""
Integration tests for leakageDetector.detectLeakage() and sendWarning(),
exercised against a real (temporary) SQLite database via the
`leakage_detector` fixture in conftest.py. sendEmail/mqtt_publish are
mocked so no network calls happen.
"""
from datetime import datetime, timedelta

import pytest


def seed_readings(db, readings):
    """
    Insert a list of (datetime, filtered_value) tuples as Reading rows.
    totalconsumption is set equal to filtered for simplicity; imageName is
    a dummy value since these tests never touch image files.
    """
    for i, (time, value) in enumerate(readings):
        db.Reading.create(
            time=time,
            totalconsumption=value,
            filtered=value,
            imageName=f"dummy_{i}.jpg",
        )


def flat_history(db, days, start_value=100.0, end_hours_ago=0, interval_minutes=15):
    """Seed `days` days of perfectly flat (zero-consumption) readings ending `end_hours_ago` hours before now."""
    now = datetime.now()
    end = now - timedelta(hours=end_hours_ago)
    n_points = days * 24 * 60 // interval_minutes
    start = end - timedelta(minutes=interval_minutes * n_points)
    readings = []
    t = start
    while t <= end:
        readings.append((t, start_value))
        t += timedelta(minutes=interval_minutes)
    seed_readings(db, readings)


class TestDetectLeakage:
    def test_no_readings_does_not_crash(self, leakage_detector):
        # Should be a no-op (empty series everywhere) rather than raising.
        leakage_detector.detectLeakage()
        assert leakage_detector.getKeyValueStoreValue("leakDebCounter", default="0") == "1"

    def test_flow_free_period_resets_counter(self, leakage_detector, db_module):
        leakage_detector.setKeyValueStoreValue("leakDebCounter", "2")
        flat_history(db_module, days=1)
        leakage_detector.detectLeakage()
        assert leakage_detector.getKeyValueStoreValue("leakDebCounter") == "0"

    def test_continuous_flow_increments_counter(self, leakage_detector, db_module):
        # 96 readings over the last 24h (15-minute sampling), each strictly
        # increasing -> no flow-free period found by the dual check.
        now = datetime.now()
        readings = [
            (now - timedelta(minutes=15 * (95 - i)), 100.0 + i * 0.01)
            for i in range(96)
        ]
        seed_readings(db_module, readings)
        leakage_detector.detectLeakage()
        assert leakage_detector.getKeyValueStoreValue("leakDebCounter") == "1"
        reasons = leakage_detector.getKeyValueStoreValue("leakDebReasons")
        assert "no flow-free period" in reasons

    def test_counter_increments_across_repeated_runs(self, leakage_detector, db_module):
        now = datetime.now()
        readings = [
            (now - timedelta(minutes=15 * (95 - i)), 100.0 + i * 0.01)
            for i in range(96)
        ]
        seed_readings(db_module, readings)
        leakage_detector.detectLeakage()
        leakage_detector.detectLeakage()
        leakage_detector.detectLeakage()
        assert leakage_detector.getKeyValueStoreValue("leakDebCounter") == "3"


class TestSendWarning:
    def test_no_warning_below_threshold(self, leakage_detector, monkeypatch):
        sent = []
        monkeypatch.setattr(leakage_detector, "sendEmail", lambda s, m: sent.append((s, m)))
        leakage_detector.setKeyValueStoreValue("leakDebCounter", "2")
        leakage_detector.sendWarning()
        assert sent == []

    def test_warning_sent_above_threshold(self, leakage_detector, monkeypatch):
        sent = []
        published = []
        monkeypatch.setattr(leakage_detector, "sendEmail", lambda s, m: sent.append((s, m)))
        monkeypatch.setattr(leakage_detector, "mqtt_publish", lambda t, p: published.append((t, p)))
        leakage_detector.setKeyValueStoreValue("leakDebCounter", "3")
        leakage_detector.sendWarning()
        assert len(sent) == 1
        assert len(published) == 1
        # Counter should be reset after warning so we don't spam every run.
        assert leakage_detector.getKeyValueStoreValue("leakDebCounter") == "0"

    def test_warning_message_includes_reasons(self, leakage_detector, monkeypatch):
        sent = []
        monkeypatch.setattr(leakage_detector, "sendEmail", lambda s, m: sent.append((s, m)))
        monkeypatch.setattr(leakage_detector, "mqtt_publish", lambda t, p: None)
        leakage_detector.setKeyValueStoreValue("leakDebCounter", "3")
        leakage_detector.setKeyValueStoreValue("leakDebReasons", "Z-score model flagged 5 anomalous reading(s)")
        leakage_detector.sendWarning()
        assert "Z-score model flagged 5 anomalous reading(s)" in sent[0][1]

    def test_creates_notification(self, leakage_detector, monkeypatch, db_module):
        monkeypatch.setattr(leakage_detector, "sendEmail", lambda s, m: None)
        monkeypatch.setattr(leakage_detector, "mqtt_publish", lambda t, p: None)
        leakage_detector.setKeyValueStoreValue("leakDebCounter", "3")
        leakage_detector.sendWarning()
        notifications = list(db_module.Notification.select())
        assert len(notifications) == 1
        assert notifications[0].i18nIdentifier == "leakdetected"
        assert notifications[0].type == "warning"


class TestEndToEndScenario:
    def test_three_consecutive_leaky_days_trigger_warning(self, leakage_detector, db_module, monkeypatch):
        """
        Simulates the real cron schedule: detectLeakage() runs once per day
        for three days with continuously increasing consumption (no rest
        period), then sendWarning() should fire on the third day.
        """
        sent = []
        monkeypatch.setattr(leakage_detector, "sendEmail", lambda s, m: sent.append((s, m)))
        monkeypatch.setattr(leakage_detector, "mqtt_publish", lambda t, p: None)

        base_value = 100.0
        for day in range(3):
            now = datetime.now() - timedelta(days=2 - day)
            readings = [
                (now - timedelta(minutes=15 * (95 - i)), base_value + i * 0.01)
                for i in range(96)
            ]
            seed_readings(db_module, readings)
            base_value += 1.44  # keep the meter monotonic across days
            leakage_detector.detectLeakage()
            leakage_detector.sendWarning()

        assert len(sent) == 1


class TestDetectSustainedHighFlow:
    @staticmethod
    def seed_burst(db):
        now = datetime.now()
        values = [100.0, 100.0, 100.0, 100.075, 100.15, 100.225, 100.3]
        seed_readings(db, [
            (now - timedelta(minutes=15 * (len(values) - 1 - i)), v)
            for i, v in enumerate(values)
        ])

    def test_burst_creates_notification_once(self, leakage_detector, db_module):
        self.seed_burst(db_module)
        leakage_detector.detectSustainedHighFlow()
        leakage_detector.detectSustainedHighFlow()
        notifications = list(db_module.Notification.select())
        assert len(notifications) == 1
        assert notifications[0].i18nIdentifier == "highflowdetected"

    def test_no_flow_creates_no_notification(self, leakage_detector, db_module):
        flat_history(db_module, days=1)
        leakage_detector.detectSustainedHighFlow()
        assert db_module.Notification.select().count() == 0

    def test_no_readings_does_not_crash(self, leakage_detector, db_module):
        leakage_detector.detectSustainedHighFlow()
        assert db_module.Notification.select().count() == 0
