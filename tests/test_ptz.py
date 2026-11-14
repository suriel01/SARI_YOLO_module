"""Hikvision ISAPI PTZ control with mocked HTTP (no camera)."""
from unittest import mock

import pytest

from sari_eyes.commands import PtzCommand
from sari_eyes.ptz import HikvisionPTZ, ManualPtz, build_ptz_xml, tracking_speed
from sari_eyes.state import NodeState


class Clock:
    t = 10.0

    def __call__(self):
        return self.t


@pytest.fixture
def ptz():
    put = mock.Mock(return_value=mock.Mock(status_code=200))
    clock = Clock()
    p = HikvisionPTZ("10.0.0.10", "op", "pw", http_put=put, run_async=lambda fn: fn(), clock=clock, state=NodeState())
    return p, put, clock


def test_move_sends_isapi_continuous_xml_with_digest_auth(ptz):
    p, put, _ = ptz
    p.move(30, -20, 5)
    url = put.call_args.args[0]
    assert url == "http://10.0.0.10/ISAPI/PTZCtrl/channels/1/continuous"
    body = put.call_args.kwargs["data"]
    assert "<pan>30</pan>" in body and "<tilt>-20</tilt>" in body and "<zoom>5</zoom>" in body
    assert put.call_args.kwargs["auth"].username == "op"
    assert p.state.ptz() == (30, -20)


def test_identical_command_is_rate_limited(ptz):
    p, put, clock = ptz
    p.move(30, 0)
    clock.t += 0.01
    p.move(30, 0)
    assert put.call_count == 1
    clock.t += 0.05
    p.move(30, 0)
    assert put.call_count == 2


def test_stop_is_always_sent(ptz):
    p, put, _ = ptz
    p.stop()
    p.stop()
    assert put.call_count == 2


def test_speeds_are_clamped_in_xml():
    assert "<pan>100</pan>" in build_ptz_xml(250, 0, 0)
    assert "<tilt>-100</tilt>" in build_ptz_xml(0, -999, 0)


def test_tracking_speed_deadzone_and_direction():
    assert tracking_speed(640, 360, 640, 360) == (0, 0)
    pan, tilt = tracking_speed(1280, 0, 640, 360)
    assert pan == 100 and tilt == 100
    pan, tilt = tracking_speed(0, 720, 640, 360)
    assert pan == -100 and tilt == -100
    assert tracking_speed(660, 360, 640, 360) == (0, 0)  # inside the 0.08 deadzone


class FakeTimer:
    created = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled = delay, fn, False
        FakeTimer.created.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def is_alive(self):
        return not self.cancelled


@pytest.mark.parametrize("action,expected", [
    ("up", (0, 50, 0)), ("down", (0, -50, 0)), ("left", (-50, 0, 0)), ("right", (50, 0, 0)),
    ("zoom_in", (0, 0, 50)), ("zoom_out", (0, 0, -50)),
])
def test_manual_pulse_moves_then_schedules_stop(action, expected):
    FakeTimer.created.clear()
    ptz = mock.Mock()
    ManualPtz(ptz, timer_factory=FakeTimer).execute(PtzCommand(action))
    ptz.move.assert_called_once_with(*expected, force=True)
    [timer] = FakeTimer.created
    assert timer.delay == 0.35 and timer.fn == ptz.stop


def test_manual_stop_cancels_pending_pulse():
    FakeTimer.created.clear()
    ptz = mock.Mock()
    m = ManualPtz(ptz, timer_factory=FakeTimer)
    m.execute(PtzCommand("left"))
    m.execute(PtzCommand("stop"))
    assert FakeTimer.created[0].cancelled
    ptz.stop.assert_called_once()


def test_manual_deltas():
    ptz = mock.Mock()
    ManualPtz(ptz, timer_factory=FakeTimer).execute(PtzCommand(None, -40, 20, 0))
    ptz.move.assert_called_once_with(-40, 20, 0, force=True)
