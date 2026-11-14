"""MQTT transport: per-node topics (broker ACL pattern sari/nodes/%u/...), optional TLS."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

from ..config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Topics:
    node_id: str

    def _t(self, kind: str) -> str:
        return f"sari/nodes/{self.node_id}/{kind}"

    @property
    def alerts(self) -> str:
        return self._t("alerts")

    @property
    def telemetry(self) -> str:
        return self._t("telemetry")

    @property
    def status(self) -> str:
        return self._t("status")

    @property
    def ptz(self) -> str:
        return self._t("ptz")

    @property
    def tracking(self) -> str:
        return self._t("tracking")

    @property
    def config(self) -> str:
        return self._t("config")


def _tls(settings: Settings) -> Optional[dict]:
    return {"ca_certs": settings.mqtt_tls_ca} if settings.mqtt_tls_ca else None


def make_client(settings: Settings, role: str) -> "mqtt.Client":
    """A configured (not yet connected) client; the username is the node id."""
    client_id = f"{settings.node_id}-{role}"
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:  # paho-mqtt < 2
        client = mqtt.Client(client_id=client_id)
    client.username_pw_set(settings.mqtt_user, settings.mqtt_password)
    if settings.mqtt_tls_ca:
        client.tls_set(ca_certs=settings.mqtt_tls_ca)
    return client


def publish_alert(settings: Settings, payload: dict, publish: Callable = mqtt_publish.single) -> bool:
    """Publish one alert on sari/nodes/<node>/alerts with QoS 1. True when the broker accepted it."""
    try:
        publish(
            topic=Topics(settings.node_id).alerts,
            payload=json.dumps(payload),
            qos=1,
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            client_id=f"{settings.node_id}-alert",
            auth={"username": settings.mqtt_user, "password": settings.mqtt_password},
            tls=_tls(settings),
            keepalive=10,
        )
    except Exception as exc:
        logger.warning("MQTT alert publish failed: %s", type(exc).__name__)
        return False
    logger.info("MQTT alert published on %s", Topics(settings.node_id).alerts)
    return True
