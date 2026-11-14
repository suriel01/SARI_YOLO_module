"""Alert payloads sent from the Eyes node to the brain.

The brain decides whether to sound the siren (spec 001): the node only reports evidence.
"""
from __future__ import annotations

import base64
import time
from typing import Optional

import cv2


def encode_snapshot(frame, max_width: int = 1280, quality: int = 80) -> Optional[str]:
    """JPEG-encode a frame (downscaled to max_width) as plain base64, or None on failure."""
    try:
        h, w = frame.shape[:2]
        if w > max_width:
            frame = cv2.resize(frame, (max_width, int(h * max_width / w)))
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    except Exception:
        return None
    return base64.b64encode(buf.tobytes()).decode("ascii") if ok else None


def build_alert_payload(
    *,
    node_id: str,
    camera_id: str,
    reason: str,
    duration: float,
    confidence: float,
    snapshot_b64: Optional[str],
    event_type: str = "intrusion",
    severity: str = "high",
    message: Optional[str] = None,
) -> dict:
    """One payload shape for REST and MQTT, carrying the snapshot exactly once."""
    duration = round(float(duration), 1)
    confidence = round(float(confidence), 2)
    return {
        "node_id": node_id,
        "module_name": node_id,
        "camara_id": camera_id,
        "event_type": event_type,
        "severity": severity,
        "reason": reason,
        "message": message or f"{event_type} ({reason}) for {duration}s at {int(confidence * 100)}% confidence",
        "confidence": confidence,
        "duration": duration,
        "timestamp": time.time(),
        "snapshot": snapshot_b64,
    }
