from unittest import mock

from conftest import base_env
from sari_eyes.config import load_settings
from sari_eyes.transport import mqtt as mqtt_transport


def test_listener_subscribes_to_own_command_topics_and_dispatches():
    client = mock.Mock()
    received = []
    with mock.patch("sari_eyes.transport.mqtt.make_client", return_value=client):
        mqtt_transport.CommandListener(load_settings(base_env()), lambda topic, raw: received.append((topic, raw)))._connect_once()
    client.connect.assert_called_once_with("10.0.0.2", 1883, keepalive=30)
    client.on_connect(client, None, None, 0)
    subs = sorted(c.args for c in client.subscribe.call_args_list)
    assert subs == sorted([("sari/nodes/Jetson-PTZ_1/ptz", 0), ("sari/nodes/Jetson-PTZ_1/tracking", 1),
                           ("sari/nodes/Jetson-PTZ_1/config", 1)])
    client.on_message(client, None, mock.Mock(topic="sari/nodes/Jetson-PTZ_1/ptz", payload=b"{}"))
    assert received == [("sari/nodes/Jetson-PTZ_1/ptz", b"{}")]


def test_handler_exception_does_not_kill_the_listener():
    client = mock.Mock()
    with mock.patch("sari_eyes.transport.mqtt.make_client", return_value=client):
        mqtt_transport.CommandListener(load_settings(base_env()), mock.Mock(side_effect=RuntimeError("x")))._connect_once()
    client.on_message(client, None, mock.Mock(topic="t", payload=b"{}"))  # must not raise
