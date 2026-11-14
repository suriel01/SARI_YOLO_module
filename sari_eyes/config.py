"""Environment configuration for the SARI Eyes node.

Every secret and every deployment address is required: a missing value stops the node with a
clear ConfigError instead of silently falling back to a baked-in default.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional


class ConfigError(RuntimeError):
    """Raised when the environment is incomplete or inconsistent. Never contains secret values."""


# Old names that earlier versions read. They are rejected so a stale .env fails loudly.
LEGACY_NAMES = {
    "MQTT_PASS": "MQTT_PASSWORD",
    "MQTT_PORT": "MQTT_BROKER_PORT",
}


def _value(env: Mapping[str, str], name: str) -> Optional[str]:
    raw = env.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def require(env: Mapping[str, str], name: str) -> str:
    value = _value(env, name)
    if value is None:
        raise ConfigError(f"Missing required environment variable {name} (see .env.example)")
    return value


def optional(env: Mapping[str, str], name: str, default: Optional[str] = None) -> Optional[str]:
    value = _value(env, name)
    return default if value is None else value


def as_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = optional(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def as_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = optional(env, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None


def _reject_legacy_names(env: Mapping[str, str]) -> None:
    for legacy, canonical in LEGACY_NAMES.items():
        if _value(env, legacy) is not None:
            raise ConfigError(f"{legacy} is no longer read; rename it to {canonical}")


_CREDENTIALS_IN_URL = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)


def redact_url(url: str) -> str:
    """Strip user:password from a URL so it can be logged."""
    return _CREDENTIALS_IN_URL.sub(r"\g<scheme>***@", url)


@dataclass(frozen=True)
class Settings:
    node_id: str
    camera_ip: str
    camera_user: str
    camera_password: str = field(repr=False)
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password: str = field(repr=False)
    telegram_token: str = field(repr=False)
    telegram_chat_id: str = field(repr=False)
    node_ip: Optional[str] = None
    confidence_threshold: float = 0.70
    stream_port: int = 8080

    @property
    def rtsp_url(self) -> str:
        return f"rtsp://{self.camera_user}:{self.camera_password}@{self.camera_ip}:554/Streaming/Channels/101"


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    _reject_legacy_names(env)
    return Settings(
        node_id=require(env, "JETSON_NODE_ID"),
        camera_ip=require(env, "CAMERA_IP"),
        camera_user=require(env, "CAMERA_USER"),
        camera_password=require(env, "CAMERA_PASS"),
        mqtt_host=require(env, "MQTT_BROKER_HOST"),
        mqtt_port=as_int(env, "MQTT_BROKER_PORT", 1883),
        mqtt_user=require(env, "MQTT_USER"),
        mqtt_password=require(env, "MQTT_PASSWORD"),
        telegram_token=require(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=require(env, "TELEGRAM_CHAT_ID"),
        node_ip=optional(env, "JETSON_NODE_IP"),
        confidence_threshold=as_float(env, "CONFIDENCE_THRESHOLD", 0.70),
        stream_port=as_int(env, "STREAM_PORT", 8080),
    )
