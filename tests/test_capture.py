"""RTSP capture with a fake OpenCV VideoCapture (no camera)."""
import numpy as np

from sari_eyes.capture import ThreadedVideoCapture, waiting_frame


class FakeCap:
    def __init__(self, frames, opened=True):
        self.frames = list(frames)
        self.opened = opened
        self.released = False

    def isOpened(self):
        return self.opened

    def grab(self):
        return bool(self.frames)

    def retrieve(self):
        return True, self.frames.pop(0)

    def release(self):
        self.released = True


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def frame(v):
    return np.full((4, 4, 3), v, dtype=np.uint8)


def test_step_stores_latest_frame_and_read_returns_copy():
    clock = Clock()
    cap = ThreadedVideoCapture("rtsp://u:p@cam/1", open_capture=lambda url: FakeCap([frame(1), frame(2)]), clock=clock)
    cap._step()
    cap._step()
    ok, f = cap.read()
    assert ok and f[0, 0, 0] == 2
    f[0, 0, 0] = 99
    assert cap.read()[1][0, 0, 0] == 2
    assert cap.last_frame_at() == 100.0


def test_stale_frame_is_reported_unavailable():
    clock = Clock()
    cap = ThreadedVideoCapture("rtsp://x", open_capture=lambda url: FakeCap([frame(1)]), clock=clock)
    cap._step()
    clock.t += 1.5
    assert cap.read() == (False, None)


def test_watchdog_reopens_a_stalled_stream():
    clock = Clock()
    opened = []
    def opener(url):
        c = FakeCap([])
        opened.append(c)
        return c
    cap = ThreadedVideoCapture("rtsp://x", open_capture=opener, clock=clock, watchdog_s=3.0)
    cap.last_frame_time = 100.0
    clock.t = 104.0
    cap._step()
    assert len(opened) == 2 and opened[0].released


def test_closed_stream_is_reopened():
    opened = []
    def opener(url):
        c = FakeCap([], opened=False)
        opened.append(c)
        return c
    cap = ThreadedVideoCapture("rtsp://x", open_capture=opener, clock=Clock(), reconnect_delay_s=0)
    cap._step()
    assert len(opened) == 2


def test_capture_logs_never_contain_credentials(caplog):
    import logging
    with caplog.at_level(logging.INFO):
        ThreadedVideoCapture("rtsp://admin:hunter2@10.0.0.10:554/x", open_capture=lambda url: FakeCap([]))
    assert "hunter2" not in caplog.text and "10.0.0.10" in caplog.text


def test_waiting_frame_is_jpeg():
    assert waiting_frame("WAIT")[:2] == b"\xff\xd8"
