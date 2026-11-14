import ast
import re
from pathlib import Path

import pytest

from conftest import ROOT, base_env
from sari_eyes.config import ConfigError, load_settings, redact_url

SECRET_NAMES = ["CAMERA_PASS", "MQTT_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SARI_NODE_KEY"]
REQUIRED_NAMES = SECRET_NAMES + ["JETSON_NODE_ID", "CAMERA_IP", "CAMERA_USER", "MQTT_BROKER_HOST", "MQTT_USER", "CEREBRO_HTTP_EVENT_URL"]


def test_valid_env_loads():
    s = load_settings(base_env())
    assert s.node_id == "Jetson-PTZ_1"
    assert s.mqtt_password == "test-mqtt-pass"
    assert s.mqtt_port == 1883


@pytest.mark.parametrize("name", REQUIRED_NAMES)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_required_variable_fails_closed(name, value):
    with pytest.raises(ConfigError) as exc:
        load_settings(base_env(**{name: value}))
    assert name in str(exc.value)


def test_error_never_echoes_other_secret_values():
    with pytest.raises(ConfigError) as exc:
        load_settings(base_env(MQTT_PASSWORD=None))
    assert "test-camera-pass" not in str(exc.value)


@pytest.mark.parametrize("legacy,canonical", [("MQTT_PASS", "MQTT_PASSWORD"), ("MQTT_PORT", "MQTT_BROKER_PORT")])
def test_legacy_name_is_rejected_with_rename_hint(legacy, canonical):
    env = base_env(**{legacy: "1883" if "PORT" in legacy else "x"})
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    assert legacy in str(exc.value) and canonical in str(exc.value)


def test_canonical_broker_port_is_read():
    assert load_settings(base_env(MQTT_BROKER_PORT="1884")).mqtt_port == 1884


def test_invalid_port_fails_closed():
    with pytest.raises(ConfigError):
        load_settings(base_env(MQTT_BROKER_PORT="abc"))


def test_rtsp_url_contains_credentials_but_redacted_form_does_not():
    s = load_settings(base_env())
    assert "test-camera-pass" in s.rtsp_url
    assert "test-camera-pass" not in redact_url(s.rtsp_url)
    assert "10.0.0.10" in redact_url(s.rtsp_url)


def test_settings_repr_hides_secrets():
    text = repr(load_settings(base_env()))
    for secret in ("test-camera-pass", "test-mqtt-pass", "test-telegram-token", "test-node-key"):
        assert secret not in text


# --- Regression guards: no secret defaults in code, compose or env template ---

SECRETISH = re.compile(r"PASS|TOKEN|SECRET|KEY|CHAT_ID")


def _python_files():
    return [p for p in ROOT.rglob("*.py") if ".venv" not in p.parts and "tests" not in p.parts]


def test_no_python_reads_a_secret_env_var_with_a_default():
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"get", "getenv"}:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and SECRETISH.search(first.value):
                default = node.args[1]
                if not (isinstance(default, ast.Constant) and default.value in (None, "")):
                    offenders.append(f"{path.name}:{node.lineno} {first.value}")
    assert offenders == []


def test_compose_has_no_inline_secret_values():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for line in compose.splitlines():
        m = re.match(r"\s*-\s*([A-Z_]+)=(.*)", line)
        if m and SECRETISH.search(m.group(1)):
            pytest.fail(f"secret variable {m.group(1)} set inline in docker-compose.yml")
        assert ":-" not in line or not SECRETISH.search(line), line


def test_env_is_gitignored_and_example_has_placeholders_only():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if SECRETISH.search(key):
            assert value.startswith("<") and value.endswith(">"), key
