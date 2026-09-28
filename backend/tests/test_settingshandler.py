"""
Tests for the developerMode default/backfill added to settingshandler.py.
"""
import json
import os


def _settings_path():
    import settingshandler
    return settingshandler.SETTINGSPATH


def test_read_settings_creates_defaults_with_developer_mode_off(tmp_settings):
    import settingshandler

    settings = settingshandler.readSettings()

    assert settings["developerMode"] is False
    # File written to disk should also carry the default, not just the
    # in-memory return value.
    with open(_settings_path()) as f:
        on_disk = json.load(f)
    assert on_disk["developerMode"] is False


def test_read_settings_backfills_missing_developer_mode(tmp_settings):
    import settingshandler

    # Simulate a settings.json written before developerMode existed.
    legacy = {
        "mqtt": {"broker": "b", "port": 1, "username": "u", "password": "p"},
        "smtp": {
            "server": "s",
            "port": 1,
            "sender": "a@b.de",
            "recipient": "a@b.de",
            "password": "p",
        },
    }
    with open(_settings_path(), "w") as f:
        json.dump(legacy, f)

    settings = settingshandler.readSettings()

    assert settings["developerMode"] is False
    assert settings["mqtt"]["broker"] == "b"


def test_read_settings_preserves_developer_mode_true(tmp_settings):
    import settingshandler

    existing = {
        "mqtt": {"broker": "b", "port": 1, "username": "u", "password": "p"},
        "smtp": {
            "server": "s",
            "port": 1,
            "sender": "a@b.de",
            "recipient": "a@b.de",
            "password": "p",
        },
        "developerMode": True,
    }
    with open(_settings_path(), "w") as f:
        json.dump(existing, f)

    settings = settingshandler.readSettings()

    assert settings["developerMode"] is True
