"""Validation of brain commands received over MQTT (ptz, tracking, config)."""
import json

import pytest

from conftest import ROOT
from sari_eyes.commands import (CONFIDENCE_MAX, CONFIDENCE_MIN, CommandError, PtzCommand, decode,
                                parse_config, parse_ptz, parse_tracking)


# --- decode ---

def test_decode_rejects_non_json_non_object_and_oversized():
    with pytest.raises(CommandError):
        decode(b"{nope")
    with pytest.raises(CommandError):
        decode(b"[1, 2]")
    with pytest.raises(CommandError):
        decode(json.dumps({"x": "a" * 5000}).encode())
    assert decode(b'{"enabled": true}') == {"enabled": True}


# --- tracking ---

@pytest.mark.parametrize("value", [True, False])
def test_tracking_accepts_booleans(value):
    assert parse_tracking({"enabled": value}) is value


@pytest.mark.parametrize("payload", [{}, {"enabled": "false"}, {"enabled": 0}, {"enabled": None}])
def test_tracking_rejects_non_boolean(payload):
    with pytest.raises(CommandError):
        parse_tracking(payload)


# --- ptz ---

@pytest.mark.parametrize("action", ["up", "down", "left", "right", "stop", "center", "zoom_in", "zoom_out", "UP"])
def test_ptz_known_actions(action):
    assert parse_ptz({"action": action}) == PtzCommand(action=action.lower())


def test_ptz_deltas_in_range():
    assert parse_ptz({"pan_delta": -100, "tilt_delta": 50.6, "zoom_delta": 0}) == PtzCommand(None, -100, 50, 0)


@pytest.mark.parametrize("payload", [
    {"action": "self_destruct"},
    {"pan_delta": 101},
    {"tilt_delta": -1000},
    {"zoom_delta": "fast"},
    {"pan_delta": True},
    {"pan_delta": float("nan")},
    {},
])
def test_ptz_rejects_invalid(payload):
    with pytest.raises(CommandError):
        parse_ptz(payload)


# --- config ---

def test_confidence_band_is_safe():
    assert (CONFIDENCE_MIN, CONFIDENCE_MAX) == (0.25, 0.9)


@pytest.mark.parametrize("value", [0.25, 0.5, 0.9])
def test_config_accepts_confidence_inside_band(value):
    assert parse_config({"yolo_confidence": value}) == {"yolo_confidence": value}


@pytest.mark.parametrize("value", [0.0, 0.1, 0.24, 0.91, 0.99, 1.5, "0.5", None, True, float("nan")])
def test_config_rejects_confidence_outside_band(value):
    with pytest.raises(CommandError):
        parse_config({"yolo_confidence": value})


def test_config_tracking_flags():
    assert parse_config({"ptz_tracking": False}) == {"tracking": False}
    assert parse_config({"active": True}) == {"tracking": True}
    with pytest.raises(CommandError):
        parse_config({"ptz_tracking": "no"})


def test_config_rejects_whole_message_when_one_field_is_invalid():
    with pytest.raises(CommandError):
        parse_config({"yolo_confidence": 0.5, "ptz_tracking": "yes"})


def test_config_ignores_unknown_keys():
    assert parse_config({"stream_url": "x", "yolo_confidence": 0.6}) == {"yolo_confidence": 0.6}


# --- WebSocket channel removed (the brain has no WebSocket server) ---

def test_no_websocket_command_channel():
    code = "\n".join(p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py")
                     if ".venv" not in p.parts and "tests" not in p.parts)
    assert "import websockets" not in code
    assert "8765" not in code
