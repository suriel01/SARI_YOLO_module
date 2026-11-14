"""Mutable node state shared by the inference loop, the MQTT command thread and PTZ control."""
from __future__ import annotations

import threading
from typing import Tuple


class NodeState:
    def __init__(self, tracking: bool = True, confidence: float = 0.70) -> None:
        self._lock = threading.Lock()
        self._tracking = tracking
        self._confidence = confidence
        self._pan = 0
        self._tilt = 0

    @property
    def tracking_enabled(self) -> bool:
        with self._lock:
            return self._tracking

    def set_tracking(self, enabled: bool) -> None:
        with self._lock:
            self._tracking = bool(enabled)

    @property
    def confidence_threshold(self) -> float:
        with self._lock:
            return self._confidence

    def set_confidence(self, value: float) -> None:
        with self._lock:
            self._confidence = float(value)

    def set_ptz(self, pan: int, tilt: int) -> None:
        with self._lock:
            self._pan, self._tilt = pan, tilt

    def ptz(self) -> Tuple[int, int]:
        with self._lock:
            return self._pan, self._tilt

    def snapshot(self) -> dict:
        with self._lock:
            return {"auto_tracking": self._tracking, "confidence_threshold": self._confidence,
                    "pan": self._pan, "tilt": self._tilt}
