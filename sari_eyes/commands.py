"""Validation of commands the brain sends over MQTT (sari/nodes/<id>/ptz|tracking|config).

The broker ACL already limits who can publish these topics; this module makes sure that
whatever arrives is well-formed and inside safe ranges before it moves a camera or changes
detection sensitivity. Invalid messages are rejected as a whole.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Optional

MAX_PAYLOAD_BYTES = 4096
PTZ_ACTIONS = {"up", "down", "left", "right", "stop", "center", "zoom_in", "zoom_out"}
PTZ_DELTA_LIMIT = 100
CONFIDENCE_MIN = 0.25
CONFIDENCE_MAX = 0.9


class CommandError(ValueError):
    """The command is malformed or out of range; it must be ignored."""


@dataclass(frozen=True)
class PtzCommand:
    action: Optional[str] = None
    pan: int = 0
    tilt: int = 0
    zoom: int = 0


def decode(raw: bytes) -> dict:
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise CommandError(f"payload larger than {MAX_PAYLOAD_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CommandError("payload is not valid JSON") from None
    if not isinstance(data, dict):
        raise CommandError("payload must be a JSON object")
    return data


def _bool(payload: dict, key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise CommandError(f"{key} must be true or false")
    return value


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CommandError(f"{name} must be a finite number")
    return float(value)


def parse_tracking(payload: dict) -> bool:
    return _bool(payload, "enabled")


def parse_ptz(payload: dict) -> PtzCommand:
    action = payload.get("action")
    if action is not None:
        if not isinstance(action, str) or action.lower() not in PTZ_ACTIONS:
            raise CommandError("unknown PTZ action")
        return PtzCommand(action=action.lower())

    deltas = {}
    for key in ("pan_delta", "tilt_delta", "zoom_delta"):
        value = _number(payload.get(key, 0), key)
        if abs(value) > PTZ_DELTA_LIMIT:
            raise CommandError(f"{key} must be within ±{PTZ_DELTA_LIMIT}")
        deltas[key] = int(value)
    if not any(k in payload for k in ("pan_delta", "tilt_delta", "zoom_delta")):
        raise CommandError("PTZ command needs an action or a delta")
    return PtzCommand(None, deltas["pan_delta"], deltas["tilt_delta"], deltas["zoom_delta"])


def parse_config(payload: dict) -> dict:
    """Returns the validated changes: {"yolo_confidence": float, "tracking": bool} (subset)."""
    changes = {}
    if "yolo_confidence" in payload:
        value = _number(payload["yolo_confidence"], "yolo_confidence")
        if not CONFIDENCE_MIN <= value <= CONFIDENCE_MAX:
            raise CommandError(f"yolo_confidence must be within {CONFIDENCE_MIN}-{CONFIDENCE_MAX}")
        changes["yolo_confidence"] = value
    for key in ("ptz_tracking", "active"):
        if key in payload:
            changes["tracking"] = _bool(payload, key)
            break
    return changes
