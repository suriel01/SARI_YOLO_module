"""Camera tamper detection: black, occluded (uniform) and frozen frames, and RTSP stream loss.

A condition must hold for ``hold_s`` seconds (``frozen_s`` for frozen frames) before it is
reported, and each reason is reported at most once per ``cooldown_s``.
"""
from __future__ import annotations

import time
import zlib
from typing import Dict, Optional

import cv2
import numpy as np

BLACK_MEAN = 15.0      # mean grey level below which the frame is considered black
UNIFORM_STD = 8.0      # grey std below which the lens is considered covered (occluded)


class TamperDetector:
    def __init__(self, hold_s: float = 3.0, frozen_s: float = 10.0, stream_loss_s: float = 10.0,
                 cooldown_s: float = 60.0) -> None:
        self.hold_s = hold_s
        self.frozen_s = frozen_s
        self.stream_loss_s = stream_loss_s
        self.cooldown_s = cooldown_s
        self.started_at = time.time()
        self._condition: Optional[str] = None
        self._since = 0.0
        self._prev_crc: Optional[int] = None
        self._prev_t = 0.0
        self._last_report: Dict[str, float] = {}

    @staticmethod
    def _small_grey(frame: np.ndarray) -> np.ndarray:
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        return cv2.resize(grey, (64, 48), interpolation=cv2.INTER_AREA).astype(np.float32)

    def _classify(self, grey: np.ndarray, crc: int) -> Optional[str]:
        if grey.mean() < BLACK_MEAN:
            return "black_frame"
        if grey.std() < UNIFORM_STD:
            return "occluded"
        # Frozen means bit-identical full-resolution frames: a live sensor (noise, OSD clock)
        # never repeats a frame exactly for frozen_s seconds, even when the scene is still.
        if crc == self._prev_crc:
            return "frozen_frame"
        return None

    def _report(self, reason: str, now: float) -> Optional[str]:
        last = self._last_report.get(reason)
        if last is not None and now - last < self.cooldown_s:
            return None
        self._last_report[reason] = now
        return reason

    def analyze(self, frame: np.ndarray, now: float) -> Optional[str]:
        """Feed one frame; returns a tamper reason when one should be reported."""
        grey = self._small_grey(frame)
        crc = zlib.crc32(np.ascontiguousarray(frame).data)
        condition = self._classify(grey, crc)
        if condition != self._condition:
            self._condition = condition
            # A frozen stream started with the previous (first identical) frame.
            self._since = self._prev_t if condition == "frozen_frame" else now
        self._prev_crc, self._prev_t = crc, now

        if condition is None:
            return None
        hold = self.frozen_s if condition == "frozen_frame" else self.hold_s
        if now - self._since >= hold:
            return self._report(condition, now)
        return None

    def check_stream(self, now: float, last_frame_time: float) -> Optional[str]:
        """Returns "rtsp_loss" when no frame arrived for stream_loss_s seconds."""
        reference = last_frame_time or self.started_at
        if now - reference > self.stream_loss_s:
            return self._report("rtsp_loss", now)
        return None
