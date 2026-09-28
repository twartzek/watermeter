"""
Tests for the pure, YOLO-result-shape logic in readTotalConsumption.py:

- getIntegerFromPredictions() / getRawDigitsFromPredictions() turn YOLO's
  raw detected classes into a digit sequence / number, with no rollover
  correction applied (that step was removed -- see outlierDetection.py
  module history/git log: resolveRolloverAmbiguity() repeatedly corrupted
  correct readings in production, e.g. 89.9774 -> 88.2482, and was deleted
  rather than patched again).

Uses a minimal fake "result" object (only boxes.cls, matching what
ResultsExtended exposes) instead of a real YOLO Results object, so this
doesn't need a camera/model and runs in the plain dev venv.
"""
import torch


class _FakeBoxes:
    def __init__(self, classes):
        # result.boxes.cls is a torch.Tensor in real ultralytics Results
        # objects (getIntegerFromPredictions calls .numpy() on it).
        self.cls = torch.tensor(classes, dtype=torch.float32)


class _FakeResult:
    def __init__(self, classes):
        self.boxes = _FakeBoxes(classes)


def test_returns_value_from_raw_digit_classes(outlier_detector):
    import readTotalConsumption as rtc

    result = _FakeResult([1, 6, 4])
    value, nBoxes = rtc.getIntegerFromPredictions(result)

    assert value == 164.0
    assert nBoxes == 3


def test_no_boxes_returns_none(outlier_detector):
    import readTotalConsumption as rtc

    result = _FakeResult([])
    value, nBoxes = rtc.getIntegerFromPredictions(result)

    assert value is None
    assert nBoxes == 0


def test_non_digit_classes_are_excluded(outlier_detector):
    import readTotalConsumption as rtc

    # Class 10 is the counter-area class (see identifyNeedlesCorrectionFactor),
    # not a digit -- must not appear in the value.
    result = _FakeResult([2, 10, 3])
    value, nBoxes = rtc.getIntegerFromPredictions(result)

    assert value == 23.0
    assert nBoxes == 2


class TestGetRawDigitsFromPredictions:
    def test_returns_raw_digit_list_unmodified(self, outlier_detector):
        import readTotalConsumption as rtc

        result = _FakeResult([4, 0, 8, 5, 0])
        assert rtc.getRawDigitsFromPredictions(result) == [4, 0, 8, 5, 0]

    def test_excludes_non_digit_classes(self, outlier_detector):
        import readTotalConsumption as rtc

        result = _FakeResult([2, 10, 3])
        assert rtc.getRawDigitsFromPredictions(result) == [2, 3]

    def test_empty_when_no_boxes(self, outlier_detector):
        import readTotalConsumption as rtc

        result = _FakeResult([])
        assert rtc.getRawDigitsFromPredictions(result) == []
