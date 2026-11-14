import threading

from sari_eyes.intrusion import IntrusionTimer
from sari_eyes.state import NodeState


def test_state_defaults_and_updates():
    s = NodeState(tracking=True, confidence=0.7)
    assert s.tracking_enabled and s.confidence_threshold == 0.7
    s.set_tracking(False)
    s.set_confidence(0.5)
    s.set_ptz(10, -20)
    assert s.snapshot() == {"auto_tracking": False, "confidence_threshold": 0.5, "pan": 10, "tilt": -20}


def test_state_is_safe_under_concurrent_writers():
    s = NodeState()
    def worker(v):
        for _ in range(2000):
            s.set_ptz(v, v)
            pan, tilt = s.ptz()
            assert pan == tilt
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]


def test_intrusion_fires_after_five_seconds_of_presence():
    t = IntrusionTimer(trigger_s=5.0, patience_s=2.0, cooldown_s=10.0)
    assert [t.update(True, now) for now in (0, 1, 2, 3, 4)] == [None] * 5
    assert t.update(True, 5.0) == 5.0


def test_intrusion_timer_tolerates_short_gaps_and_resets_after_patience():
    t = IntrusionTimer(trigger_s=5.0, patience_s=2.0, cooldown_s=10.0)
    t.update(True, 0.0)
    t.update(False, 1.5)          # short gap: keep counting
    assert t.update(True, 5.0) == 5.0
    t2 = IntrusionTimer(trigger_s=5.0, patience_s=2.0, cooldown_s=10.0)
    t2.update(True, 0.0)
    t2.update(False, 3.0)         # gap longer than patience: reset
    assert t2.update(True, 5.0) is None


def test_intrusion_cooldown_between_alerts():
    t = IntrusionTimer(trigger_s=5.0, patience_s=2.0, cooldown_s=10.0)
    t.update(True, 0.0)
    assert t.update(True, 5.0) == 5.0
    fired = [now for now in range(6, 30) if t.update(True, float(now))]
    assert fired and fired[0] >= 15.0


def test_alerting_flag_for_annotation():
    t = IntrusionTimer(trigger_s=5.0)
    t.update(True, 0.0)
    assert not t.is_alerting(4.0) and t.is_alerting(5.0)
