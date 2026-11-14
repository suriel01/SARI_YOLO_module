"""Jetson hardware telemetry agent: MQTT (retained status + LWT) with a keyed HTTP fallback.

Runs on the Jetson host (systemd unit deploy/sari-jetson.service), separate from the vision
container. Unknown metrics are reported as null instead of invented numbers.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

import psutil
import requests

from .config import ConfigError, as_int, optional, require
from .transport.mqtt import Topics

logger = logging.getLogger("sari_eyes.telemetry")

NODE_KEY_HEADER = "X-SARI-Node-Key"
THERMAL_ZONES = [f"sys/devices/virtual/thermal/thermal_zone{i}/temp" for i in range(5)]
GPU_LOAD_FILES = [
    "sys/devices/gpu.0/load",
    "sys/devices/platform/17000000.gpu/load",
    "sys/class/devfreq/17000000.ga10b/device/load",
    "sys/class/devfreq/17000000.ga10b/load",
]


@dataclass(frozen=True)
class TelemetrySettings:
    node_id: str
    name: str
    ip: str
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password: str = field(repr=False)
    node_key: str = field(repr=False)
    http_url: str
    mqtt_tls_ca: Optional[str] = None
    interval_s: float = 3.0


def load_telemetry_settings(env: Optional[Mapping[str, str]] = None) -> TelemetrySettings:
    env = os.environ if env is None else env
    for legacy, canonical in (("MQTT_PASS", "MQTT_PASSWORD"), ("MQTT_PORT", "MQTT_BROKER_PORT")):
        if optional(env, legacy) is not None:
            raise ConfigError(f"{legacy} is no longer read; rename it to {canonical}")
    node_id = require(env, "JETSON_NODE_ID")
    mqtt_user = require(env, "MQTT_USER")
    if mqtt_user != node_id:
        raise ConfigError("MQTT_USER must equal JETSON_NODE_ID (broker ACL pattern sari/nodes/%u/...)")
    tls_ca = optional(env, "MQTT_TLS_CA")
    if tls_ca is not None and not os.path.isfile(tls_ca):
        raise ConfigError("MQTT_TLS_CA must point to an existing CA certificate file")
    return TelemetrySettings(
        node_id=node_id,
        name=optional(env, "JETSON_NODE_NAME", node_id),
        ip=optional(env, "JETSON_NODE_IP", ""),
        mqtt_host=require(env, "MQTT_BROKER_HOST"),
        mqtt_port=as_int(env, "MQTT_BROKER_PORT", 8883 if tls_ca else 1883),
        mqtt_user=mqtt_user,
        mqtt_password=require(env, "MQTT_PASSWORD"),
        node_key=require(env, "SARI_NODE_KEY"),
        http_url=require(env, "CEREBRO_HTTP_TELEMETRY_URL"),
        mqtt_tls_ca=tls_ca,
    )


def _read_float(path: Path) -> Optional[float]:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def collect_metrics(node_id: str, name: str, ip: str, sysfs_root="/") -> dict:
    root = Path(sysfs_root)
    temp_c = None
    for rel in THERMAL_ZONES:
        value = _read_float(root / rel)
        if value is not None:
            value = value / 1000.0 if value > 1000 else value
            if 20.0 <= value <= 105.0:
                temp_c = round(value, 1)
                break
    gpu = None
    for rel in GPU_LOAD_FILES:
        value = _read_float(root / rel)
        if value is not None:
            gpu = round(value / 10.0 if value > 100 else value, 1)  # Tegra reports per-mille
            break
    mem = psutil.virtual_memory()
    return {
        "node_id": node_id,
        "name": name,
        "ip": ip,
        "ram_used_gb": round((mem.total - mem.available) / 1024 ** 3, 2),
        "ram_total_gb": round(mem.total / 1024 ** 3, 2),
        "cpu_load_pct": round(psutil.cpu_percent(interval=None), 1),
        "gpu_load_pct": gpu,
        "temp_c": temp_c,
        "fps": None,
        "link_status": None,
    }


def send_http(settings: TelemetrySettings, payload: dict, timeout: float = 2.0) -> bool:
    try:
        response = requests.post(settings.http_url, json=payload, headers={NODE_KEY_HEADER: settings.node_key},
                                 timeout=timeout, allow_redirects=False)
    except Exception as exc:
        logger.warning("HTTP telemetry fallback failed: %s", type(exc).__name__)
        return False
    if response.status_code == 200:
        return True
    logger.warning("HTTP telemetry fallback rejected (HTTP %s)", response.status_code)
    return False


def make_mqtt_client(settings: TelemetrySettings):
    import paho.mqtt.client as mqtt
    client_id = f"{settings.node_id}-telemetry"
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        client = mqtt.Client(client_id=client_id)
    client.username_pw_set(settings.mqtt_user, settings.mqtt_password)
    if settings.mqtt_tls_ca:
        client.tls_set(ca_certs=settings.mqtt_tls_ca)
    return client


class TelemetryAgent:
    def __init__(self, settings: TelemetrySettings) -> None:
        self.settings = settings
        self.topics = Topics(settings.node_id)
        self.connected = False
        self.client = make_mqtt_client(settings)
        self.client.will_set(self.topics.status, json.dumps(
            {"node_id": settings.node_id, "status": "offline", "reason": "connection_lost"}), qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0 or str(rc) == "Success":
            self.connected = True
            client.publish(self.topics.status, json.dumps({
                "node_id": self.settings.node_id, "status": "online",
                "ip": self.settings.ip, "name": self.settings.name}), qos=1, retain=True)
            logger.info("Telemetry connected to %s:%s", self.settings.mqtt_host, self.settings.mqtt_port)
        else:
            self.connected = False
            logger.warning("Telemetry MQTT connection refused (rc=%s)", rc)

    def _on_disconnect(self, *args, **kwargs):
        self.connected = False
        logger.warning("Telemetry MQTT disconnected")

    def _connect_loop(self) -> None:
        while True:
            if not self.connected:
                try:
                    self.client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=15)
                    self.client.loop_start()
                except Exception as exc:
                    logger.debug("Telemetry MQTT reconnect failed: %s", type(exc).__name__)
            time.sleep(5.0)

    def publish(self, payload: dict) -> Optional[str]:
        """Returns the transport used ("mqtt" or "http") or None when both failed."""
        if self.connected:
            try:
                if self.client.publish(self.topics.telemetry, json.dumps(payload), qos=0).rc == 0:
                    return "mqtt"
            except Exception as exc:
                logger.warning("Telemetry MQTT publish failed: %s", type(exc).__name__)
        return "http" if send_http(self.settings, payload) else None

    def run_forever(self) -> None:
        threading.Thread(target=self._connect_loop, daemon=True, name="TelemetryMQTT").start()
        while True:
            s = self.settings
            self.publish(collect_metrics(s.node_id, s.name, s.ip))
            time.sleep(s.interval_s)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        settings = load_telemetry_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    logger.info("Starting telemetry agent for %s", settings.node_id)
    TelemetryAgent(settings).run_forever()
