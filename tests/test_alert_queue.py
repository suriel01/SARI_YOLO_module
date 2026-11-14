"""Persistent alert queue and delivery dispatcher (retry/backoff, Telegram fallback)."""
import json
import logging
from unittest import mock

import pytest

from sari_eyes.alert_queue import AlertDispatcher, DiskAlertQueue


@pytest.fixture
def q(tmp_path):
    return DiskAlertQueue(tmp_path / "queue", max_items=5)


def test_put_survives_restart(tmp_path):
    DiskAlertQueue(tmp_path / "q").put({"event_type": "intrusion", "n": 1})
    reopened = DiskAlertQueue(tmp_path / "q")
    items = reopened.pending()
    assert len(items) == 1 and items[0].payload == {"event_type": "intrusion", "n": 1}
    assert items[0].attempts == 0 and items[0].telegram_sent is False


def test_ack_removes_item_and_leaves_no_temp_files(q, tmp_path):
    item_id = q.put({"n": 1})
    q.ack(item_id)
    assert q.pending() == []
    assert list((tmp_path / "queue").iterdir()) == []


def test_pending_is_oldest_first(q):
    ids = [q.put({"n": i}) for i in range(3)]
    assert [i.id for i in q.pending()] == ids


def test_capacity_evicts_oldest_loudly(q, caplog):
    with caplog.at_level(logging.ERROR):
        for i in range(6):
            q.put({"n": i})
    assert [i.payload["n"] for i in q.pending()] == [1, 2, 3, 4, 5]
    assert "evicted" in caplog.text


def test_corrupted_entry_is_quarantined_not_lost_silently(q, tmp_path, caplog):
    q.put({"n": 1})
    (tmp_path / "queue" / "0000-broken.json").write_text("{not json")
    with caplog.at_level(logging.ERROR):
        assert [i.payload["n"] for i in q.pending()] == [1]
    assert (tmp_path / "queue" / "0000-broken.json.bad").exists()
    assert "corrupt" in caplog.text


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _dispatcher(q, rest=False, mqtt=False, telegram=True, **kw):
    clock = Clock()
    d = AlertDispatcher(
        q,
        deliver=[mock.Mock(return_value=rest, __name__="rest"), mock.Mock(return_value=mqtt, __name__="mqtt")],
        telegram=mock.Mock(return_value=telegram),
        node_id="Jetson-PTZ_1",
        clock=clock,
        **kw,
    )
    return d, clock


def test_delivered_by_rest_is_acked_without_mqtt_or_telegram(q):
    d, _ = _dispatcher(q, rest=True)
    d.submit({"event_type": "intrusion"})
    d.process_once()
    assert q.pending() == []
    d.deliver[1].assert_not_called()
    d.telegram.assert_not_called()


def test_rest_failure_falls_back_to_mqtt(q):
    d, _ = _dispatcher(q, rest=False, mqtt=True)
    d.submit({"event_type": "intrusion"})
    d.process_once()
    assert q.pending() == []
    d.deliver[0].assert_called_once()
    d.deliver[1].assert_called_once()


def test_undelivered_alert_stays_queued_with_backoff(q):
    d, clock = _dispatcher(q, backoff_base=2.0, backoff_max=10.0)
    d.submit({"event_type": "intrusion"})
    d.process_once()
    [item] = q.pending()
    assert item.attempts == 1 and item.next_attempt == clock.t + 2.0

    d.process_once()  # not due yet: no new attempt
    assert d.deliver[0].call_count == 1

    for expected_delay in (4.0, 8.0, 10.0, 10.0):
        clock.t = q.pending()[0].next_attempt
        d.process_once()
        assert q.pending()[0].next_attempt == clock.t + expected_delay


def test_telegram_fallback_once_when_brain_stays_unreachable(q):
    d, clock = _dispatcher(q, telegram_after_s=30.0, backoff_base=5.0, backoff_max=5.0)
    d.submit({"event_type": "intrusion", "reason": "person_over_5s", "message": "m"})
    for _ in range(12):
        d.process_once()
        clock.t += 5.0
    d.telegram.assert_called_once()
    text = d.telegram.call_args.args[0]
    assert "Jetson-PTZ_1" in text and "intrusion" in text
    assert q.pending()[0].telegram_sent is True


def test_failed_telegram_is_retried_on_later_attempts(q):
    d, clock = _dispatcher(q, telegram=False, telegram_after_s=0.0, backoff_base=1.0, backoff_max=1.0)
    d.submit({"event_type": "tamper"})
    for _ in range(3):
        d.process_once()
        clock.t += 1.0
    assert d.telegram.call_count == 3
    assert q.pending()[0].telegram_sent is False


def test_late_brain_recovery_delivers_and_removes(q):
    d, clock = _dispatcher(q, backoff_base=1.0)
    d.submit({"event_type": "intrusion"})
    d.process_once()
    d.deliver[0].return_value = True
    clock.t += 1.0
    d.process_once()
    assert q.pending() == []


def test_transport_exception_counts_as_failure(q):
    d, _ = _dispatcher(q)
    d.deliver[0].side_effect = RuntimeError("boom")
    d.submit({"event_type": "intrusion"})
    d.process_once()
    assert q.pending()[0].attempts == 1
