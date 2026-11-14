"""Decides when a sustained person detection becomes an intrusion alert."""
from __future__ import annotations

from typing import Optional


class IntrusionTimer:
    """Fires after ``trigger_s`` of presence; gaps shorter than ``patience_s`` keep counting;
    at most one alert per ``cooldown_s``."""

    def __init__(self, trigger_s: float = 5.0, patience_s: float = 2.0, cooldown_s: float = 10.0) -> None:
        self.trigger_s = trigger_s
        self.patience_s = patience_s
        self.cooldown_s = cooldown_s
        self._started: Optional[float] = None
        self._last_seen: Optional[float] = None
        self._last_alert = float("-inf")

    def update(self, seen: bool, now: float) -> Optional[float]:
        """Returns the presence duration when an alert must be sent, else None."""
        if not seen:
            if self._last_seen is not None and now - self._last_seen > self.patience_s:
                self._started = self._last_seen = None
            return None

        self._last_seen = now
        if self._started is None:
            self._started = now
            return None
        elapsed = now - self._started
        if elapsed >= self.trigger_s and now - self._last_alert > self.cooldown_s:
            self._last_alert = now
            self._started = self._last_seen = None
            return elapsed
        return None

    def is_alerting(self, now: float) -> bool:
        return self._started is not None and now - self._started >= self.trigger_s
