"""Command handling and startup wiring of the node (no camera, no broker)."""
import json
import logging
from unittest import mock

import pytest

from sari_eyes import app
from sari_eyes.state import NodeState
from sari_eyes.transport.mqtt import Topics

T = Topics("Jetson-PTZ_1")


@pytest.fixture
def ctx():
    return SimpleCtx()


class SimpleCtx:
    def __init__(self):
        self.state = NodeState(tracking=True, confidence=0.7)
        self.ptz = mock.Mock()
        self.manual = mock.Mock()


def handle(ctx, topic, payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return app.handle_command(topic, raw, T, ctx.state, ctx.ptz, ctx.manual)


def test_config_updates_confidence_inside_band(ctx):
    assert handle(ctx, T.config, {"yolo_confidence": 0.5}) is True
    assert ctx.state.confidence_threshold == 0.5


def test_out_of_band_confidence_is_rejected_and_state_unchanged(ctx, caplog):
    with caplog.at_level(logging.WARNING):
        assert handle(ctx, T.config, {"yolo_confidence": 0.1}) is False
    assert ctx.state.confidence_threshold == 0.7
    assert "rejected" in caplog.text


def test_tracking_off_stops_camera(ctx):
    assert handle(ctx, T.tracking, {"enabled": False}) is True
    assert ctx.state.tracking_enabled is False
    ctx.ptz.stop.assert_called_once()


def test_ptz_command_goes_to_manual_controller(ctx):
    handle(ctx, T.ptz, {"action": "left"})
    assert ctx.manual.execute.call_args.args[0].action == "left"


def test_invalid_json_is_rejected(ctx):
    assert handle(ctx, T.ptz, b"{bad") is False
    ctx.manual.execute.assert_not_called()


def test_unknown_topic_ignored(ctx):
    assert handle(ctx, "sari/nodes/other/ptz", {"action": "left"}) is False


def test_stream_bind_failure_does_not_crash_the_node(caplog):
    with mock.patch("sari_eyes.app.start_stream_server", side_effect=OSError("in use")):
        with caplog.at_level(logging.ERROR):
            assert app.start_stream_safely(mock.Mock(stream_token="t" * 16, stream_bind="0.0.0.0", stream_port=8080),
                                           mock.Mock()) is None
    assert "stream server" in caplog.text.lower()


def test_entrypoint_is_thin():
    from conftest import ROOT
    src = (ROOT / "camara_ptz.py").read_text(encoding="utf-8")
    assert len(src.splitlines()) < 30 and "from sari_eyes.app import main" in src


def test_no_print_calls_in_package():
    import ast
    from conftest import ROOT
    for path in (ROOT / "sari_eyes").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
                pytest.fail(f"print() in {path.name}:{node.lineno}")


class FakeDetector:
    def __init__(self, dets):
        self.dets = dets

    def detect(self, frame, threshold):
        return self.dets


def make_node(monkeypatch, tmp_path):
    import numpy as np
    from conftest import base_env
    from sari_eyes.config import load_settings
    from sari_eyes.detector import Detection
    monkeypatch.setenv("ALERT_QUEUE_DIR", str(tmp_path / "q"))
    node = app.EyesNode(load_settings(base_env()))
    node.ptz = mock.Mock()
    node.dispatcher = mock.Mock()
    xs = np.linspace(0, 255, 640, dtype=np.float32)
    ys = np.linspace(0, 255, 360, dtype=np.float32)[:, None]
    frame = np.dstack([(xs + ys) / 2] * 3).astype(np.uint8)  # structured scene, not uniform
    return node, frame, Detection(0.88, (560, 0, 640, 100))  # top-right person, off centre


def test_process_tracks_target_and_alerts_once_after_five_seconds(monkeypatch, tmp_path):
    node, frame, person = make_node(monkeypatch, tmp_path)
    for second in range(6):
        frame[0, 0, 0] = second  # live sensor: frames are never bit-identical
        node.process(frame.copy(), FakeDetector([person]), now=float(second))
    pan, tilt = node.ptz.move.call_args.args[:2]
    assert pan > 0 and tilt > 0
    intrusions = [c.args[0] for c in node.dispatcher.submit.call_args_list if c.args[0]["event_type"] == "intrusion"]
    assert len(intrusions) == 1
    assert intrusions[0]["confidence"] == 0.88 and intrusions[0]["snapshot"]
    assert node.frames.get()[:2] == b"\xff\xd8"


def test_process_respects_tracking_pause(monkeypatch, tmp_path):
    node, frame, person = make_node(monkeypatch, tmp_path)
    node.state.set_tracking(False)
    node.process(frame, FakeDetector([person]))
    node.ptz.move.assert_not_called()


def test_process_uses_live_confidence_threshold(monkeypatch, tmp_path):
    node, frame, person = make_node(monkeypatch, tmp_path)
    seen = []
    class Spy(FakeDetector):
        def detect(self, frame, threshold):
            seen.append(threshold)
            return []
    node.state.set_confidence(0.42)
    node.process(frame, Spy([]))
    assert seen == [0.42]
