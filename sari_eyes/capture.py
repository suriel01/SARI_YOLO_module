"""Low-latency RTSP capture on its own thread, with a reconnect watchdog."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional, Tuple

import cv2
import numpy as np

from .config import redact_url

# TCP transport avoids artefacts and black screens on Hikvision streams.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

logger = logging.getLogger(__name__)


def _open_capture(url: str):
    cap = cv2.VideoCapture(url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def waiting_frame(message: str = "CONNECTING TO CAMERA...") -> bytes:
    """Synthetic JPEG served while the camera has not delivered frames yet."""
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(img, (0, 0), (1280, 720), (15, 23, 42), -1)
    cv2.circle(img, (640, 360), 60, (56, 189, 248), 2)
    cv2.line(img, (640, 280), (640, 440), (56, 189, 248), 2)
    cv2.line(img, (560, 360), (720, 360), (56, 189, 248), 2)
    cv2.putText(img, "SARI EYES (JETSON ORIN)", (440, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (56, 189, 248), 2)
    cv2.putText(img, message, (320, 460), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    cv2.putText(img, time.strftime("%Y-%m-%d %H:%M:%S"), (540, 500), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (148, 163, 184), 1)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else b""


class ThreadedVideoCapture:
    """Keeps only the newest frame so inference never processes a stale backlog."""

    def __init__(
        self,
        rtsp_url: str,
        open_capture: Callable[[str], object] = _open_capture,
        clock: Callable[[], float] = time.time,
        stale_after_s: float = 1.0,
        watchdog_s: float = 3.0,
        reconnect_delay_s: float = 1.0,
    ) -> None:
        self.rtsp_url = rtsp_url
        self._open = open_capture
        self._clock = clock
        self.stale_after_s = stale_after_s
        self.watchdog_s = watchdog_s
        self.reconnect_delay_s = reconnect_delay_s
        self.lock = threading.Lock()
        self.frame: Optional[np.ndarray] = None
        self.last_frame_time = 0.0
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.cap = None
        self._reopen()

    def _reopen(self) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                logger.debug("release of the previous capture failed", exc_info=True)
        logger.info("Opening RTSP stream %s", redact_url(self.rtsp_url))
        self.cap = self._open(self.rtsp_url)

    def _step(self) -> None:
        """One iteration of the reader loop (separated for tests)."""
        if self.cap is None or not self.cap.isOpened():
            logger.warning("RTSP stream unavailable; reconnecting")
            time.sleep(self.reconnect_delay_s)
            self._reopen()
            return
        if not self.cap.grab():
            time.sleep(0.01)
            if self._clock() - self.last_frame_time > self.watchdog_s:
                logger.warning("RTSP stream stalled for more than %ss; reconnecting", self.watchdog_s)
                self._reopen()
            return
        ok, frame = self.cap.retrieve()
        if ok and frame is not None:
            with self.lock:
                self.frame = frame
                self.last_frame_time = self._clock()

    def _run(self) -> None:
        while self.running:
            try:
                self._step()
            except Exception:
                logger.exception("RTSP reader iteration failed")
                time.sleep(0.5)

    def start(self) -> "ThreadedVideoCapture":
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True, name="RTSPReaderThread")
            self.thread.start()
        return self

    def last_frame_at(self) -> float:
        with self.lock:
            return self.last_frame_time

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        with self.lock:
            if self.frame is None or self._clock() - self.last_frame_time > self.stale_after_s:
                return False, None
            return True, self.frame.copy()

    def stop(self) -> None:
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if self.cap is not None:
            self.cap.release()
        logger.info("RTSP capture stopped")
