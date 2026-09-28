"""
Tests for the developer-mode debug info added to /readings/last (see
restapi.py: _buildDebugInfo, _buildFilterSteps, _imageUrlFor):

- The raw YOLO value, needle/digit box counts, and discard reason (if the
  reading was discarded by missingNeedleOrDigitDetector) are surfaced so
  the developer mode can show WHY a reading has no totalconsumption.
- Each step of the outlier-filter pipeline (missingDigitDetector ->
  maxFlowDetector -> negativeDeltaDetector, which never runs for a
  discarded reading) is surfaced with before/after values and whether that
  step actually changed it, so the developer mode can show WHICH step (if
  any) adjusted a reading.
- The bbox-annotated photo is used whenever it exists on disk, even for a
  discarded reading (which still has boxes -- it just failed the count
  check), falling back to the plain original only when no boxes were
  detected at all.
"""
import os

import pytest


def _make_settings(restapi, developer_mode: bool):
    return restapi.Settings(
        mqtt=restapi.Mqtt(broker="b", port=1, username="u", password="p"),
        smtp=restapi.Smtp(
            server="s", port=1, sender="a@b.de", recipient="a@b.de", password="p"
        ),
        developerMode=developer_mode,
    )


def test_debug_info_absent_outside_developer_mode(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, False))

    from db import store_reading
    store_reading(12.3, 12.3, "ok.jpg")

    reading = restapi_module.getLastReading()

    assert reading.debugInfo is None


def test_debug_info_present_in_developer_mode(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(
        85.6379, 85.6379, "ok.jpg",
        nNeedlesDetected=4, nDigitsDetected=5,
        beforeMissingNeedleCheck=85.6379,
    )

    reading = restapi_module.getLastReading()

    assert reading.debugInfo is not None
    assert reading.debugInfo.nNeedlesDetected == 4
    assert reading.debugInfo.nDigitsDetected == 5
    assert reading.debugInfo.rawValue == 85.6379
    assert reading.debugInfo.discardReason is None


def test_debug_info_shows_discard_reason_for_discarded_reading(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(
        None, None, "discarded.jpg",
        nNeedlesDetected=3, nDigitsDetected=5,
        beforeMissingNeedleCheck=85.639,
        discardReason="needles: expected 4 (learned from history), got 3.",
    )

    reading = restapi_module.getLastReading()

    assert reading.totalconsumption is None
    assert reading.debugInfo.rawValue == 85.639
    assert reading.debugInfo.discardReason == "needles: expected 4 (learned from history), got 3."


def test_debug_info_filter_steps_show_which_filter_changed_the_value(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    # maxFlowDetector clipped the value (85.9 -> 85.6), the other two steps
    # left it unchanged.
    store_reading(
        85.9, 85.6, "ok.jpg",
        afterMissingDigit=85.9, afterMaxFlow=85.6, afterNegativeDelta=85.6,
    )

    reading = restapi_module.getLastReading()
    steps = {s.name: s for s in reading.debugInfo.filterSteps}

    assert steps["missingDigitDetector"].changed is False
    assert steps["missingDigitDetector"].before == 85.9
    assert steps["missingDigitDetector"].after == 85.9

    assert steps["maxFlowDetector"].changed is True
    assert steps["maxFlowDetector"].before == 85.9
    assert steps["maxFlowDetector"].after == 85.6

    assert steps["negativeDeltaDetector"].changed is False
    assert steps["negativeDeltaDetector"].before == 85.6
    assert steps["negativeDeltaDetector"].after == 85.6


def test_debug_info_filter_steps_empty_when_not_recorded(restapi_module):
    # A discarded reading (or an older row from before these columns
    # existed) never ran the filter pipeline -- filterSteps must be empty,
    # not a chain of Nones.
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(None, None, "discarded.jpg", discardReason="something")

    reading = restapi_module.getLastReading()

    assert reading.debugInfo.filterSteps == []


def test_debug_info_filter_steps_present_when_pipeline_ran(restapi_module):
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    from db import store_reading
    store_reading(
        85.6379, 85.6379, "ok.jpg",
        afterMissingDigit=85.6379, afterMaxFlow=85.6379, afterNegativeDelta=85.6379,
    )

    reading = restapi_module.getLastReading()
    steps = {s.name: s for s in reading.debugInfo.filterSteps}

    assert set(steps.keys()) == {
        "missingDigitDetector", "maxFlowDetector", "negativeDeltaDetector",
    }


def test_bbox_image_used_when_it_exists(restapi_module, images_dir):
    restapi_module.storeSettings(_make_settings(restapi_module, False))

    open(os.path.join(images_dir, "ok.jpg"), "w").close()
    open(os.path.join(images_dir, "ok.jpg_bbox.jpg"), "w").close()

    from db import store_reading
    store_reading(12.3, 12.3, "ok.jpg")

    reading = restapi_module.getLastReading()

    assert reading.imageUrl.endswith("ok.jpg_bbox.jpg")


def test_original_image_used_when_no_bbox_exists(restapi_module, images_dir):
    # A reading discarded before any box was ever detected (flowNeedles/flow
    # is None) never gets a _bbox.jpg -- falls back to the plain original.
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    open(os.path.join(images_dir, "nodetection.jpg"), "w").close()

    from db import store_reading
    store_reading(None, None, "nodetection.jpg")

    reading = restapi_module.getLastReading()

    assert reading.imageUrl.endswith("nodetection.jpg")
    assert not reading.imageUrl.endswith("_bbox.jpg")


def test_bbox_image_used_for_a_discarded_reading_with_detected_boxes(restapi_module, images_dir):
    # missingNeedleOrDigitDetector discards AFTER the bbox image is already
    # saved (see readTotalConsumption.py:_gettotalconsumption) -- the
    # developer mode should still show the annotated photo so the missing
    # needle/digit box is visible.
    restapi_module.storeSettings(_make_settings(restapi_module, True))

    open(os.path.join(images_dir, "discarded.jpg"), "w").close()
    open(os.path.join(images_dir, "discarded.jpg_bbox.jpg"), "w").close()

    from db import store_reading
    store_reading(
        None, None, "discarded.jpg",
        nNeedlesDetected=3, nDigitsDetected=5,
        beforeMissingNeedleCheck=85.639,
        discardReason="needles: expected 4, got 3.",
    )

    reading = restapi_module.getLastReading()

    assert reading.imageUrl.endswith("discarded.jpg_bbox.jpg")
