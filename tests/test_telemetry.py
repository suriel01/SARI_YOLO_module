"""Unified Jetson telemetry agent (MQTT with keyed HTTP fallback)."""
import json
from unittest import mock

import pytest

from sari_eyes import telemetry
from sari_eyes.config import ConfigError

ENV = {
    "JETSON_NODE_ID": "Jetson-PTZ_1",
    "MQTT_BROKER_HOST": "10.0.0.2",
    "MQTT_USER": "Jetson-PTZ_1",
    "MQTT_PASSWORD": "test-mqtt-pass",
    "SARI_NODE_KEY": "test-node-key",
    "CEREBRO_HTTP_TELEMETRY_URL": "http://10.0.0.2:7000/api/telemetry/node",
}


def test_settings_fail_closed_and_require_user_equal_node():
    s = telemetry.load_telemetry_settings(ENV)
    assert s.node_id == "Jetson-PTZ_1" and s.mqtt_port == 1883 and s.name == "Jetson-PTZ_1"
    for missing in ("JETSON_NODE_ID", "MQTT_PASSWORD", "SARI_NODE_KEY", "CEREBRO_HTTP_TELEMETRY_URL"):
        with pytest.raises(ConfigError):
            telemetry.load_telemetry_settings({k: v for k, v in ENV.items() if k != missing})
    with pytest.raises(ConfigError):
        telemetry.load_telemetry_settings({**ENV, "MQTT_USER": "sari_operator"})
    with pytest.raises(ConfigError):
        telemetry.load_telemetry_settings({**ENV, "MQTT_PASS": "x"})


def test_metrics_report_unknown_values_as_none_not_fake_numbers(tmp_path):
    m = telemetry.collect_metrics("Jetson-PTZ_1", "Jetson", "10.0.0.5", sysfs_root=tmp_path)
    assert m["node_id"] == "Jetson-PTZ_1"
    assert m["gpu_load_pct"] is None and m["temp_c"] is None and m["fps"] is None
    assert m["ram_total_gb"] > 0


def test_metrics_read_jetson_sysfs(tmp_path):
    (tmp_path / "sys/devices/virtual/thermal/thermal_zone0").mkdir(parents=True)
    (tmp_path / "sys/devices/virtual/thermal/thermal_zone0/temp").write_text("51500\n")
    (tmp_path / "sys/devices/gpu.0").mkdir(parents=True)
    (tmp_path / "sys/devices/gpu.0/load").write_text("455\n")
    m = telemetry.collect_metrics("N", "N", "", sysfs_root=tmp_path)
    assert m["temp_c"] == 51.5 and m["gpu_load_pct"] == 45.5


def test_http_fallback_sends_node_key_to_single_url():
    s = telemetry.load_telemetry_settings(ENV)
    with mock.patch("sari_eyes.telemetry.requests.post", return_value=mock.Mock(status_code=200)) as post:
        assert telemetry.send_http(s, {"node_id": "Jetson-PTZ_1"}) is True
    post.assert_called_once()
    assert post.call_args.args[0] == ENV["CEREBRO_HTTP_TELEMETRY_URL"]
    assert post.call_args.kwargs["headers"] == {"X-SARI-Node-Key": "test-node-key"}
    assert post.call_args.kwargs["allow_redirects"] is False


def test_http_fallback_failure_is_false():
    s = telemetry.load_telemetry_settings(ENV)
    with mock.patch("sari_eyes.telemetry.requests.post", side_effect=OSError):
        assert telemetry.send_http(s, {}) is False


def test_lwt_and_birth_are_on_the_node_status_topic():
    s = telemetry.load_telemetry_settings(ENV)
    client = mock.Mock()
    with mock.patch("sari_eyes.telemetry.make_mqtt_client", return_value=client):
        agent = telemetry.TelemetryAgent(s)
    topic, payload = client.will_set.call_args.args[:2]
    assert topic == "sari/nodes/Jetson-PTZ_1/status"
    assert json.loads(payload)["node_id"] == "Jetson-PTZ_1" and json.loads(payload)["status"] == "offline"
    agent._on_connect(client, None, None, 0)
    topic, payload = client.publish.call_args.args[:2]
    assert topic == "sari/nodes/Jetson-PTZ_1/status" and json.loads(payload)["status"] == "online"


def test_publish_falls_back_to_http_when_mqtt_is_down():
    s = telemetry.load_telemetry_settings(ENV)
    with mock.patch("sari_eyes.telemetry.make_mqtt_client", return_value=mock.Mock()):
        agent = telemetry.TelemetryAgent(s)
    agent.connected = False
    with mock.patch("sari_eyes.telemetry.send_http", return_value=True) as http:
        assert agent.publish({"node_id": "Jetson-PTZ_1"}) == "http"
    http.assert_called_once()
