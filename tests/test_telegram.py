from unittest import mock

from sari_eyes import telegram


def test_send_without_credentials_fails_closed_without_network(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with mock.patch("sari_eyes.telegram.requests.post") as post:
        assert telegram.send_message("hello") is False
    post.assert_not_called()


def test_send_uses_explicit_credentials():
    response = mock.Mock(status_code=200)
    with mock.patch("sari_eyes.telegram.requests.post", return_value=response) as post:
        assert telegram.send_message("hello", token="tkn", chat_id="42") is True
    url = post.call_args.args[0]
    assert url.endswith("/bottkn/sendMessage")
    assert post.call_args.kwargs["json"]["chat_id"] == "42"


def test_failure_log_does_not_leak_token(caplog):
    with mock.patch("sari_eyes.telegram.requests.post", side_effect=Exception("boom https://api.telegram.org/botSECRETTKN/x")):
        assert telegram.send_message("hello", token="SECRETTKN", chat_id="42") is False
    assert "SECRETTKN" not in caplog.text


def _run_cli(*args, env_extra=None):
    import os
    import subprocess
    import sys

    from conftest import ROOT
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(ROOT / "telegram_alert.py"), *args],
                          capture_output=True, text=True, env=env, cwd=ROOT, timeout=30)


def test_cli_without_arguments_prints_spanish_usage_and_exits_0():
    result = _run_cli()
    assert result.returncode == 0
    assert result.stdout.strip() == "Uso: python3 telegram_alert.py 'Mensaje de prueba'"


def test_cli_send_failure_is_printed_and_still_exits_0():
    result = _run_cli("hola")
    assert result.returncode == 0
    assert "[TELEGRAM]" in result.stdout + result.stderr
