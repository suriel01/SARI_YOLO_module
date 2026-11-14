import ast
import re
from pathlib import Path

import pytest

from conftest import ROOT, base_env, project_python_files
from sari_eyes.config import ConfigError, load_settings, redact_url

SECRET_NAMES = ["CAMERA_PASS", "MQTT_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SARI_NODE_KEY", "EYES_STREAM_TOKEN"]
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
    for secret in ("test-camera-pass", "test-mqtt-pass", "test-telegram-token", "test-node-key", "test-stream-token-0123456789"):
        assert secret not in text


# --- Regression guards: no secret defaults in code, compose or env template ---

SECRETISH = re.compile(r"PASS|TOKEN|SECRET|KEY|CHAT_ID")


def _python_files():
    return project_python_files()


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


def test_deploy_env_template_has_placeholders_only():
    for line in (ROOT / "deploy" / "jetson.env.example").read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if SECRETISH.search(key):
            assert value.startswith("<") and value.endswith(">"), key


def test_systemd_unit_reads_secrets_from_environment_file_only():
    unit = (ROOT / "deploy" / "sari-jetson.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/sari/jetson.env" in unit
    for line in unit.splitlines():
        if line.startswith("Environment="):
            assert not SECRETISH.search(line), line


def test_dockerfile_pins_base_and_runs_unprivileged():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base = next(l for l in dockerfile.splitlines() if l.startswith("FROM "))
    assert "latest" not in base and "@sha256:" in base
    assert any(l.strip() == "USER sari" for l in dockerfile.splitlines())
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            assert "==" in line, line


def test_no_credential_shaped_strings_in_project_text_files():
    telegram_token = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")
    files = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".md", ".py", ".yml", ".yaml", ".example", ".service", ".txt", ""}
             and not ({".venv", ".git", "tests", "model_cache", "alert_queue", ".atl"} & set(p.relative_to(ROOT).parts))
             and p.name != ".env"]
    offenders = [str(p.relative_to(ROOT)) for p in files
                 if telegram_token.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert offenders == []


def test_docs_never_write_credential_values():
    # e.g. "Clave `s3cr3t...`" or "Valor actual del sistema: `123:ABC...`"; placeholders and VAR names are fine
    pattern = re.compile(r"(?i)(clave|contraseña|password|token|valor actual)[^`\n]{0,30}[:=]?\s*`(?!<)(?![A-Z_]+`)[^`\s./]{8,}`")
    offenders = []
    for path in list(ROOT.glob("*.md")) + list((ROOT / "spec").glob("*.md")) + list((ROOT / "docs").glob("*.md")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{n}")
    assert offenders == []
