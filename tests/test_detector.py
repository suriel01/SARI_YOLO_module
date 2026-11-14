"""Person detection with a fake YOLO model (no GPU, no weights)."""
from types import SimpleNamespace

import numpy as np

from sari_eyes import detector
from sari_eyes.detector import Detection, PersonDetector, pick_target


class Box:
    def __init__(self, conf, xyxy):
        self.conf = [conf]
        self.xyxy = [SimpleNamespace(tolist=lambda: list(xyxy))]


class FakeModel:
    def __init__(self, boxes):
        self.boxes = boxes
        self.calls = []

    def __call__(self, frame, **kw):
        self.calls.append(kw)
        return [SimpleNamespace(boxes=self.boxes)]


def test_detect_filters_by_threshold_and_requests_person_class_only():
    model = FakeModel([Box(0.9, (0, 0, 10, 10)), Box(0.4, (5, 5, 20, 20))])
    dets = PersonDetector(model, device="cpu").detect(np.zeros((10, 10, 3)), threshold=0.5)
    assert dets == [Detection(0.9, (0, 0, 10, 10))]
    assert model.calls[0]["classes"] == [0] and model.calls[0]["device"] == "cpu"


def test_detect_handles_no_boxes():
    assert PersonDetector(FakeModel(None), device="cpu").detect(np.zeros((2, 2, 3)), 0.5) == []


def test_pick_target_nearest_to_centre():
    a, b = Detection(0.9, (0, 0, 10, 10)), Detection(0.8, (90, 90, 110, 110))
    assert pick_target([a, b], 100, 100) is b
    assert pick_target([], 100, 100) is None
    assert b.center == (100, 100)


def test_load_model_without_ultralytics_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "YOLO", None)
    assert detector.load_model(tmp_path) is None


def test_load_model_prefers_cached_tensorrt_engine(monkeypatch, tmp_path):
    (tmp_path / "yolo26n.engine").write_bytes(b"x")
    loaded = []
    monkeypatch.setattr(detector, "YOLO", lambda path, **kw: loaded.append((path, kw)) or "engine-model")
    assert detector.load_model(tmp_path) == "engine-model"
    assert loaded == [(str(tmp_path / "yolo26n.engine"), {"task": "detect"})]
