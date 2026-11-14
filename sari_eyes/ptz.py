"""Hikvision ISAPI PTZ control: continuous moves, auto-tracking speeds and manual pulses."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Tuple

import requests
from requests.auth import HTTPDigestAuth

from .commands import PtzCommand
from .state import NodeState

logger = logging.getLogger(__name__)

SPEED_LIMIT = 100
MIN_INTERVAL_S = 0.04      # 25 Hz command rate
TRACKING_DEADZONE = 0.08
PULSE_SPEED = 50
PULSE_S = 0.35


def _clamp(value: float) -> int:
    return int(max(-SPEED_LIMIT, min(SPEED_LIMIT, value)))


def build_ptz_xml(pan: float, tilt: float, zoom: float) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<PTZData version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">\n'
        f"    <pan>{_clamp(pan)}</pan>\n    <tilt>{_clamp(tilt)}</tilt>\n    <zoom>{_clamp(zoom)}</zoom>\n"
        "</PTZData>"
    )


def _background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="PTZSend").start()


class HikvisionPTZ:
    def __init__(self, ip: str, username: str, password: str, channel: int = 1, timeout: float = 2.0,
                 state: Optional[NodeState] = None, http_put: Callable = requests.put,
                 run_async: Callable[[Callable[[], None]], None] = _background,
                 clock: Callable[[], float] = time.time) -> None:
        self.url = f"http://{ip}/ISAPI/PTZCtrl/channels/{channel}/continuous"
        self.auth = HTTPDigestAuth(username, password)
        self.timeout = timeout
        self.state = state or NodeState()
        self._put = http_put
        self._run_async = run_async
        self._clock = clock
        self._lock = threading.Lock()
        self._last = (None, None, None)
        self._last_sent = float("-inf")

    def move(self, pan: float, tilt: float, zoom: float = 0, force: bool = False) -> bool:
        pan, tilt, zoom = _clamp(pan), _clamp(tilt), _clamp(zoom)
        force = force or (pan == 0 and tilt == 0 and zoom == 0)
        now = self._clock()
        with self._lock:
            if not force and (pan, tilt, zoom) == self._last and now - self._last_sent < MIN_INTERVAL_S:
                return True
            self._last, self._last_sent = (pan, tilt, zoom), now
        self.state.set_ptz(pan, tilt)
        body = build_ptz_xml(pan, tilt, zoom)

        def send() -> None:
            try:
                response = self._put(self.url, data=body, headers={"Content-Type": "application/xml"},
                                     auth=self.auth, timeout=self.timeout)
                if response.status_code not in (200, 201):
                    logger.warning("PTZ command rejected by the camera (HTTP %s)", response.status_code)
            except requests.RequestException as exc:
                logger.warning("PTZ command failed: %s", type(exc).__name__)

        self._run_async(send)
        return True

    def stop(self) -> bool:
        return self.move(0, 0, 0, force=True)


def tracking_speed(px: float, py: float, cx: float, cy: float,
                   deadzone: float = TRACKING_DEADZONE) -> Tuple[int, int]:
    """Pan/tilt speed that recentres a target at (px, py); zero inside the deadzone."""
    def axis(norm: float) -> int:
        if abs(norm) <= deadzone:
            return 0
        sign = 1 if norm > 0 else -1
        dist = (min(abs(norm), 1.0) - deadzone) / (1.0 - deadzone)
        return _clamp(sign * (20 + (dist ** 1.1) * 80))

    return axis((px - cx) / cx), axis((cy - py) / cy)


class ManualPtz:
    """Operator PTZ commands: D-pad pulses auto-stop after PULSE_S seconds."""

    def __init__(self, ptz: HikvisionPTZ, timer_factory: Callable = threading.Timer) -> None:
        self.ptz = ptz
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._timer = None

    def execute(self, cmd: PtzCommand) -> None:
        with self._lock:
            if self._timer is not None and self._timer.is_alive():
                self._timer.cancel()
            self._timer = None

        if cmd.action in ("stop", "center"):
            self.ptz.stop()
            return

        pan, tilt, zoom = cmd.pan, cmd.tilt, cmd.zoom
        if cmd.action is not None:
            pan, tilt, zoom = {
                "up": (0, PULSE_SPEED, 0),
                "down": (0, -PULSE_SPEED, 0),
                "left": (-PULSE_SPEED, 0, 0),
                "right": (PULSE_SPEED, 0, 0),
                "zoom_in": (0, 0, PULSE_SPEED),
                "zoom_out": (0, 0, -PULSE_SPEED),
            }[cmd.action]

        self.ptz.move(pan, tilt, zoom, force=True)
        if pan or tilt or zoom:
            timer = self._timer_factory(PULSE_S, self.ptz.stop)
            timer.daemon = True
            with self._lock:
                self._timer = timer
            timer.start()
