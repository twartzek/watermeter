"""
Shared pytest setup for the backend test suite.

Several backend modules read configuration at *import time* (db.py,
settingshandler.py, mqtt.py, mylog.py all call dotenv_values("watermeter/.env")
or readSettings() as soon as they're imported), and open a live SQLite
connection / MQTT settings file as a side effect. Since pytest imports test
modules (and therefore `from leakageDetector import ...`) during collection,
before any fixture has a chance to run, the isolated "watermeter/" project
layout has to be built and chdir'd into from `pytest_configure` -- the
earliest hook that still runs before collection.
"""
import os
import tempfile

import pytest


_tmpdir_handle = None


def pytest_configure(config):
    global _tmpdir_handle
    _tmpdir_handle = tempfile.TemporaryDirectory(prefix="watermeter_test_")
    project_root = _tmpdir_handle.name

    watermeter_dir = os.path.join(project_root, "watermeter")
    data_dir = os.path.join(project_root, "data")
    images_dir = os.path.join(data_dir, "images")
    log_dir = os.path.join(project_root, "log")
    for d in (watermeter_dir, data_dir, images_dir, log_dir):
        os.makedirs(d, exist_ok=True)

    db_path = os.path.join(data_dir, "watermeter_test.db")
    settings_path = os.path.join(data_dir, "settings.json")

    env_content = (
        f"db={db_path}\n"
        f"images={images_dir}\n"
        f"log={log_dir}\n"
        f"venv={os.path.join(project_root, 'venv')}\n"
        f"mainpath={project_root}\n"
        f"model_digits={os.path.join(project_root, 'models', 'digits', 'best.pt')}\n"
        f"model_needles={os.path.join(project_root, 'models', 'totalandneedles', 'best.pt')}\n"
        f"settingspath={settings_path}\n"
        f"frontend={os.path.join(project_root, 'frontend', 'dist')}\n"
    )
    with open(os.path.join(watermeter_dir, ".env"), "w") as f:
        f.write(env_content)

    os.chdir(project_root)


@pytest.fixture
def db_module():
    """Import db.py (schema freshly created) against the isolated test environment."""
    import db
    db.create_db_if_not_exists()
    db.db.connect(reuse_if_open=True)
    db.db.create_tables([db.Reading, db.Notification, db.KeyValueStore, db.MeterReplacement], safe=True)
    # Start each test with empty tables regardless of what earlier tests left behind.
    db.Reading.delete().execute()
    db.Notification.delete().execute()
    db.KeyValueStore.delete().execute()
    db.MeterReplacement.delete().execute()
    yield db


@pytest.fixture
def images_dir(db_module):
    """
    Path to the isolated test environment's images directory (db.py's
    config["images"]), guaranteed empty at the start of the test -- for
    tests that need real files on disk (e.g. get_newest_image()'s directory
    scan), not just DB rows.
    """
    path = db_module.config["images"]
    for f in os.listdir(path):
        os.remove(os.path.join(path, f))
    return path


@pytest.fixture
def leakage_detector(db_module, monkeypatch):
    """
    Import leakageDetector.py against the isolated environment, with
    sendEmail/mqtt_publish replaced by no-op mocks so tests never touch the
    network.
    """
    import leakageDetector as ld
    monkeypatch.setattr(ld, "sendEmail", lambda subject, message: None)
    monkeypatch.setattr(ld, "mqtt_publish", lambda topic, payload: None)
    return ld


@pytest.fixture
def tmp_settings():
    """
    Ensure settings.json doesn't exist yet, so readSettings() writes a
    fresh default file instead of reusing whatever an earlier test left
    behind.
    """
    import settingshandler
    if os.path.exists(settingshandler.SETTINGSPATH):
        os.remove(settingshandler.SETTINGSPATH)
    yield settingshandler.SETTINGSPATH
    if os.path.exists(settingshandler.SETTINGSPATH):
        os.remove(settingshandler.SETTINGSPATH)


@pytest.fixture
def restapi_module(db_module, tmp_settings):
    """
    Import restapi.py (the FastAPI route functions, called directly as
    plain Python functions -- none of them declare Depends/Request, so this
    exercises the same code the HTTP layer would run) against the isolated
    environment, with a fresh settings.json for each test.
    """
    import restapi
    return restapi


@pytest.fixture
def outlier_detector(db_module, monkeypatch):
    """
    Import outlierDetection.py against the isolated environment, with
    sendEmail replaced by a no-op mock so tests never touch the network.
    """
    import outlierDetection as od
    monkeypatch.setattr(od, "sendEmail", lambda subject, message: None)
    return od
