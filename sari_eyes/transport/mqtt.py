"""MQTT transport: per-node topics (broker ACL pattern sari/nodes/%u/...), optional TLS."""
from __future__ import annotations

import json
import logging
import threading
import time
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


class CommandListener:
    """Subscribes to this node's command topics and forwards raw payloads to ``handler``.

    Reconnects forever; a failing handler is logged and never stops the listener.
    """

    def __init__(self, settings: Settings, handler: Callable[[str, bytes], object],
                 reconnect_delay_s: float = 5.0) -> None:
        self.settings = settings
        self.handler = handler
        self.topics = Topics(settings.node_id)
        self.reconnect_delay_s = reconnect_delay_s

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0 or str(rc) == "Success":
            client.subscribe(self.topics.ptz, 0)
            client.subscribe(self.topics.tracking, 1)
            client.subscribe(self.topics.config, 1)
            logger.info("Command listener connected; subscribed to %s/{ptz,tracking,config}",
                        f"sari/nodes/{self.settings.node_id}")
        else:
            logger.warning("Command listener connection refused (rc=%s)", rc)

    def _on_message(self, client, userdata, msg):
        try:
            self.handler(msg.topic, msg.payload)
        except Exception:
            logger.exception("Command handler failed for %s", msg.topic)

    def _connect_once(self):
        client = make_client(self.settings, "ctrl")
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30)
        return client

    def _run(self) -> None:
        while True:
            try:
                self._connect_once().loop_forever()
            except Exception as exc:
                logger.warning("Command listener disconnected (%s); retrying in %ss",
                               type(exc).__name__, self.reconnect_delay_s)
            time.sleep(self.reconnect_delay_s)

    def start(self) -> "CommandListener":
        threading.Thread(target=self._run, daemon=True, name="MQTTCommandThread").start()
        return self
