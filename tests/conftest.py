import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def base_env(**overrides):
    """Minimal valid environment with obvious placeholder values (never real secrets)."""
    env = {
        "JETSON_NODE_ID": "Jetson-PTZ_1",
        "CAMERA_IP": "10.0.0.10",
        "CAMERA_USER": "viewer",
        "CAMERA_PASS": "test-camera-pass",
        "MQTT_BROKER_HOST": "10.0.0.2",
        "MQTT_USER": "Jetson-PTZ_1",
        "MQTT_PASSWORD": "test-mqtt-pass",
        "TELEGRAM_BOT_TOKEN": "test-telegram-token",
        "TELEGRAM_CHAT_ID": "12345",
        "SARI_NODE_KEY": "test-node-key",
        "CEREBRO_HTTP_EVENT_URL": "http://10.0.0.2:7000/api/alerts/event",
        "EYES_STREAM_TOKEN": "test-stream-token-0123456789",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}
