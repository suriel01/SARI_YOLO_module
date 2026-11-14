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
DELTA_ACTIONS = {None, "move", "drag"}   # proportional / click-and-drag commands from the UI
PTZ_SPEED_LIMIT = 100                    # camera speed range; larger drag deltas are clamped
PTZ_DELTA_INPUT_LIMIT = 1000             # anything beyond this is a malformed command
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
    except (UnicodeDecodeError, ValueError, RecursionError):
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
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise CommandError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise CommandError(f"{name} must be a finite number")
    return number


def parse_tracking(payload: dict) -> bool:
    return _bool(payload, "enabled")


def parse_ptz(payload: dict) -> PtzCommand:
    action = payload.get("action")
    if action is not None and not isinstance(action, str):
        raise CommandError("unknown PTZ action")
    action = action.lower() if action is not None else None
    if action not in DELTA_ACTIONS:
        if action not in PTZ_ACTIONS:
            raise CommandError("unknown PTZ action")
        return PtzCommand(action=action)

    keys = ("pan_delta", "tilt_delta", "zoom_delta")
    if not any(payload.get(k) is not None for k in keys):
        raise CommandError("PTZ command needs an action or a delta")
    speeds = []
    for key in keys:
        raw = payload.get(key)
        value = 0.0 if raw is None else _number(raw, key)
        if abs(value) > PTZ_DELTA_INPUT_LIMIT:
            raise CommandError(f"{key} must be within ±{PTZ_DELTA_INPUT_LIMIT}")
        speeds.append(int(max(-PTZ_SPEED_LIMIT, min(PTZ_SPEED_LIMIT, value))))
    return PtzCommand(None, *speeds)


def parse_config(payload: dict) -> dict:
    """Returns the validated changes: {"yolo_confidence": float, "tracking": bool} (subset)."""
    changes = {}
    for key in ("yolo_confidence", "confidence_threshold"):  # the brain publishes confidence_threshold
        if key in payload:
            value = _number(payload[key], key)
            if not CONFIDENCE_MIN <= value <= CONFIDENCE_MAX:
                raise CommandError(f"{key} must be within {CONFIDENCE_MIN}-{CONFIDENCE_MAX}")
            changes["yolo_confidence"] = value
    for key in ("ptz_tracking", "active"):
        if key in payload:
            changes["tracking"] = _bool(payload, key)
            break
    return changes
