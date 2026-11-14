"""Alignment with the hardened brain contract (per-node REST key, per-node MQTT topics)."""
import json
import re
from unittest import mock

import numpy as np
import pytest

from conftest import ROOT, base_env
from sari_eyes import alerts
from sari_eyes.config import ConfigError, load_settings
from sari_eyes.transport import mqtt as mqtt_transport
from sari_eyes.transport import rest


@pytest.fixture
def settings():
    return load_settings(base_env())


# --- config ---

def test_node_key_and_event_url_are_loaded(settings):
    assert settings.node_key == "test-node-key"
    assert settings.cerebro_http_event_url == "http://10.0.0.2:7000/api/alerts/event"


def test_mqtt_user_must_equal_node_id():
    with pytest.raises(ConfigError) as exc:
        load_settings(base_env(MQTT_USER="sari_operator"))
    assert "MQTT_USER" in str(exc.value) and "JETSON_NODE_ID" in str(exc.value)


def test_tls_ca_switches_default_port_to_8883(tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("placeholder")
    s = load_settings(base_env(MQTT_TLS_CA=str(ca)))
    assert s.mqtt_tls_ca == str(ca) and s.mqtt_port == 8883
    assert load_settings(base_env(MQTT_TLS_CA=str(ca), MQTT_BROKER_PORT="8884")).mqtt_port == 8884


def test_tls_ca_must_exist(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(base_env(MQTT_TLS_CA=str(tmp_path / "missing.crt")))


def test_without_tls_default_port_is_1883(settings):
    assert settings.mqtt_tls_ca is None and settings.mqtt_port == 1883


# --- payload ---

def test_alert_payload_carries_one_snapshot_and_no_siren_flag():
    payload = alerts.build_alert_payload(
        node_id="Jetson-PTZ_1", camera_id="PTZ_1", reason="person_over_5s",
        duration=6.0, confidence=0.83, snapshot_b64="QUJDREVGRw==",
    )
    assert "auto_siren" not in payload
    assert json.dumps(payload).count("QUJDREVGRw==") == 1
    assert payload["snapshot"] == "QUJDREVGRw=="
    assert payload["node_id"] == "Jetson-PTZ_1"
    assert payload["event_type"] == "intrusion"
    assert payload["confidence"] == 0.83


def test_encode_snapshot_returns_base64_jpeg():
    frame = np.zeros((40, 60, 3), dtype=np.uint8)
    b64 = alerts.encode_snapshot(frame)
    import base64
    assert base64.b64decode(b64)[:2] == b"\xff\xd8"


# --- REST ---

def test_rest_alert_sends_node_key_to_the_single_configured_url(settings):
    with mock.patch("sari_eyes.transport.rest.requests.post", return_value=mock.Mock(status_code=200)) as post:
        assert rest.post_alert(settings, {"x": 1}) is True
    post.assert_called_once()
    assert post.call_args.args[0] == "http://10.0.0.2:7000/api/alerts/event"
    assert post.call_args.kwargs["headers"]["X-SARI-Node-Key"] == "test-node-key"


@pytest.mark.parametrize("status", [401, 403, 500])
def test_rest_alert_rejected_is_not_delivered(settings, status):
    with mock.patch("sari_eyes.transport.rest.requests.post", return_value=mock.Mock(status_code=status)) as post:
        assert rest.post_alert(settings, {"x": 1}) is False
    post.assert_called_once()


def test_rest_network_error_is_not_delivered_and_key_not_logged(settings, caplog):
    with mock.patch("sari_eyes.transport.rest.requests.post", side_effect=OSError("down")):
        assert rest.post_alert(settings, {"x": 1}) is False
    assert "test-node-key" not in caplog.text


# --- MQTT ---

def test_topics_are_per_node():
    t = mqtt_transport.Topics("Jetson-PTZ_1")
    assert t.alerts == "sari/nodes/Jetson-PTZ_1/alerts"
    assert t.telemetry == "sari/nodes/Jetson-PTZ_1/telemetry"
    assert t.status == "sari/nodes/Jetson-PTZ_1/status"
    assert (t.ptz, t.tracking, t.config) == (
        "sari/nodes/Jetson-PTZ_1/ptz", "sari/nodes/Jetson-PTZ_1/tracking", "sari/nodes/Jetson-PTZ_1/config")


def test_mqtt_alert_goes_to_node_topic_with_qos1(settings):
    publish = mock.Mock()
    assert mqtt_transport.publish_alert(settings, {"node_id": "Jetson-PTZ_1"}, publish=publish) is True
    kwargs = publish.call_args.kwargs
    assert kwargs["topic"] == "sari/nodes/Jetson-PTZ_1/alerts"
    assert kwargs["qos"] == 1
    assert kwargs["auth"] == {"username": "Jetson-PTZ_1", "password": "test-mqtt-pass"}
    assert kwargs["tls"] is None and kwargs["port"] == 1883


def test_mqtt_alert_uses_tls_when_ca_configured(tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("placeholder")
    publish = mock.Mock()
    mqtt_transport.publish_alert(load_settings(base_env(MQTT_TLS_CA=str(ca))), {}, publish=publish)
    assert publish.call_args.kwargs["tls"] == {"ca_certs": str(ca)}
    assert publish.call_args.kwargs["port"] == 8883


def test_mqtt_alert_failure_is_reported(settings):
    assert mqtt_transport.publish_alert(settings, {}, publish=mock.Mock(side_effect=OSError("down"))) is False


def test_client_factory_sets_credentials_and_tls(tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("placeholder")
    client = mock.Mock()
    with mock.patch("sari_eyes.transport.mqtt.mqtt.Client", return_value=client):
        mqtt_transport.make_client(load_settings(base_env(MQTT_TLS_CA=str(ca))), "ctrl")
    client.username_pw_set.assert_called_once_with("Jetson-PTZ_1", "test-mqtt-pass")
    client.tls_set.assert_called_once_with(ca_certs=str(ca))


# --- static guards ---

def _code():
    return "\n".join(p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py")
                     if ".venv" not in p.parts and "tests" not in p.parts)


def test_no_hardcoded_brain_fallback_hosts_or_port_8000():
    code = _code()
    for needle in ("192.168.55.100", "192.168.1.71", ":8000"):
        assert needle not in code, needle


def test_no_publish_to_legacy_shared_alert_topic():
    assert not re.search(r"""["']sari/alerts["']""", _code())
