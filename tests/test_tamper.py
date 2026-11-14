"""Camera tamper detection: black, occluded and frozen frames, RTSP loss."""
import numpy as np
import pytest

from sari_eyes.tamper import TamperDetector


def scene(seed):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)


@pytest.fixture
def det():
    return TamperDetector(hold_s=3.0, frozen_s=10.0, stream_loss_s=10.0, cooldown_s=60.0)


def run(det, frames, start=0.0, step=1.0):
    events, t = [], start
    for f in frames:
        r = det.analyze(f, t)
        if r:
            events.append((r, t))
        t += step
    return events, t


def test_normal_changing_scene_raises_nothing(det):
    assert run(det, [scene(i) for i in range(30)])[0] == []


def test_black_frame_held_raises_once_then_cooldown(det):
    black = np.zeros((120, 160, 3), dtype=np.uint8)
    events, _ = run(det, [black] * 20)
    assert events == [("black_frame", 3.0)]


def test_short_black_glitch_is_ignored(det):
    black = np.zeros((120, 160, 3), dtype=np.uint8)
    assert run(det, [black, black, scene(1), scene(2), black, scene(3)])[0] == []


def test_uniform_covered_lens_is_occluded(det):
    grey = np.full((120, 160, 3), 128, dtype=np.uint8)
    assert run(det, [grey] * 5)[0] == [("occluded", 3.0)]


def test_identical_frames_are_frozen(det):
    still = scene(7)
    events, _ = run(det, [still] * 15)
    assert events == [("frozen_frame", 10.0)]


def test_cooldown_expires_and_alert_repeats(det):
    black = np.zeros((120, 160, 3), dtype=np.uint8)
    events, _ = run(det, [black] * 70)
    assert [e[0] for e in events] == ["black_frame", "black_frame"]
    assert events[1][1] - events[0][1] >= 60.0


def test_rtsp_loss_after_threshold(det):
    assert det.check_stream(now=105.0, last_frame_time=100.0) is None
    assert det.check_stream(now=111.0, last_frame_time=100.0) == "rtsp_loss"
    assert det.check_stream(now=112.0, last_frame_time=100.0) is None  # cooldown


def test_rtsp_never_connected_counts_from_start(det):
    det.started_at = 0.0
    assert det.check_stream(now=11.0, last_frame_time=0.0) == "rtsp_loss"


def test_tamper_payload_shape():
    from sari_eyes.alerts import build_tamper_payload
    p = build_tamper_payload(node_id="Jetson-PTZ_1", camera_id="PTZ_1", reason="rtsp_loss", snapshot_b64=None)
    assert p["event_type"] == "tamper" and p["reason"] == "rtsp_loss" and p["node_id"] == "Jetson-PTZ_1"
    assert p["severity"] == "high" and "auto_siren" not in p and p["snapshot"] is None


def test_live_still_scene_with_sensor_noise_is_not_frozen(det):
    rng = np.random.default_rng(3)
    base = scene(11)
    frames = []
    for _ in range(30):
        f = base.copy()
        ys, xs = rng.integers(0, 120, 5), rng.integers(0, 160, 5)
        f[ys, xs] ^= 1  # a handful of one-level noise flips, invisible after downscaling
        frames.append(f)
    assert run(det, frames)[0] == []
