"""Persistent alert queue with retry/backoff and a direct Telegram fallback.

Alerts are written to disk before any delivery attempt, so a brain outage or a node restart
never drops them silently. Each pending alert is retried with exponential backoff over the
configured transports (REST first, then MQTT). When the brain stays unreachable for
``telegram_after_s`` seconds, one direct Telegram message warns the operator while the brain
retries continue.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    id: str
    payload: dict
    created: float
    attempts: int = 0
    next_attempt: float = 0.0
    telegram_sent: bool = False


class DiskAlertQueue:
    """One JSON file per alert, written atomically (temp file + fsync + rename)."""

    def __init__(self, directory, max_items: int = 500) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_items = max_items
        self._lock = threading.Lock()
        self._last_ns = 0

    def _next_id(self) -> str:
        self._last_ns = max(time.time_ns(), self._last_ns + 1)
        return f"{self._last_ns:020d}-{uuid.uuid4().hex[:8]}"

    def _path(self, item_id: str) -> Path:
        return self.dir / f"{item_id}.json"

    def _write(self, item: QueueItem) -> None:
        tmp = self.dir / f".{item.id}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(item), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path(item.id))

    def _files(self) -> List[Path]:
        return sorted(self.dir.glob("*.json"))

    def put(self, payload: dict, now: Optional[float] = None) -> str:
        with self._lock:
            now = time.time() if now is None else now
            item = QueueItem(id=self._next_id(), payload=payload, created=now, next_attempt=now)
            self._write(item)
            files = self._files()
            for old in files[: max(0, len(files) - self.max_items)]:
                logger.error("Alert queue full (%s items): evicted oldest undelivered alert %s",
                             self.max_items, old.stem)
                old.unlink(missing_ok=True)
            return item.id

    def pending(self) -> List[QueueItem]:
        items = []
        with self._lock:
            for path in self._files():
                try:
                    items.append(QueueItem(**json.loads(path.read_text(encoding="utf-8"))))
                except (ValueError, TypeError) as exc:
                    logger.error("Alert queue entry %s is corrupt (%s); quarantined as .bad",
                                 path.name, type(exc).__name__)
                    path.rename(path.with_name(path.name + ".bad"))
        return items

    def update(self, item: QueueItem) -> None:
        with self._lock:
            if self._path(item.id).exists():
                self._write(item)

    def ack(self, item_id: str) -> None:
        with self._lock:
            self._path(item_id).unlink(missing_ok=True)


def _fallback_text(node_id: str, item: QueueItem) -> str:
    p = item.payload
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.created))
    return (
        f"SARI Eyes {node_id}: {p.get('event_type', 'alert')} ({p.get('reason', '-')}) since {since}. "
        f"The brain is unreachable, so this alert is sent directly. {p.get('message', '')}"
    ).strip()


class AlertDispatcher:
    def __init__(
        self,
        queue: DiskAlertQueue,
        *,
        deliver: Sequence[Callable[[dict], bool]],
        telegram: Callable[[str], bool],
        node_id: str,
        clock: Callable[[], float] = time.time,
        backoff_base: float = 2.0,
        backoff_max: float = 60.0,
        telegram_after_s: float = 30.0,
        poll_s: float = 1.0,
    ) -> None:
        self.queue = queue
        self.deliver = list(deliver)
        self.telegram = telegram
        self.node_id = node_id
        self.clock = clock
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.telegram_after_s = telegram_after_s
        self.poll_s = poll_s
        self._wake = threading.Event()
        self._stop = threading.Event()

    def submit(self, payload: dict) -> str:
        item_id = self.queue.put(payload, now=self.clock())
        self._wake.set()
        return item_id

    def _try_deliver(self, payload: dict) -> bool:
        for transport in self.deliver:
            try:
                if transport(payload):
                    return True
            except Exception as exc:
                logger.warning("Alert transport %s raised %s", getattr(transport, "__name__", transport),
                               type(exc).__name__)
        return False

    def process_once(self) -> None:
        now = self.clock()
        for item in self.queue.pending():
            if item.next_attempt > now:
                continue
            if self._try_deliver(item.payload):
                self.queue.ack(item.id)
                logger.info("Alert %s delivered after %s failed attempt(s)", item.id, item.attempts)
                continue
            item.attempts += 1
            item.next_attempt = now + min(self.backoff_base * 2 ** (item.attempts - 1), self.backoff_max)
            if not item.telegram_sent and now - item.created >= self.telegram_after_s:
                try:
                    item.telegram_sent = bool(self.telegram(_fallback_text(self.node_id, item)))
                except Exception as exc:
                    logger.warning("Telegram fallback raised %s", type(exc).__name__)
            logger.warning("Alert %s undelivered (attempt %s); next retry in %.0fs",
                           item.id, item.attempts, item.next_attempt - now)
            self.queue.update(item)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception:
                logger.exception("Alert dispatcher iteration failed")
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def start(self) -> "AlertDispatcher":
        threading.Thread(target=self._run, daemon=True, name="AlertDispatcher").start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
