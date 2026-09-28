"""
Tests for the /api/v1/notifications endpoints in restapi.py, called
directly as plain Python functions per the restapi_module fixture in
conftest.py (see test_restapi_readings.py for the same pattern).
"""
from datetime import datetime


def test_delete_all_notifications_removes_every_row(restapi_module, db_module):
    db_module.addNotification("Leak", "leakdetected", "warning")
    db_module.addNotification("High flow", "highflowdetected", "warning")
    assert len(list(db_module.Notification.select())) == 2

    result = restapi_module.deleteAllNotificationsApi()

    assert result == {"ok": True}
    assert list(db_module.Notification.select()) == []


def test_delete_all_notifications_on_empty_table_is_a_noop(restapi_module, db_module):
    result = restapi_module.deleteAllNotificationsApi()

    assert result == {"ok": True}
    assert list(db_module.Notification.select()) == []


def test_get_notifications_endpoint_reflects_deletion(restapi_module, db_module):
    db_module.addNotification("Leak", "leakdetected", "warning")

    assert len(restapi_module.getNotifications()) == 1

    restapi_module.deleteAllNotificationsApi()

    assert restapi_module.getNotifications() == []
