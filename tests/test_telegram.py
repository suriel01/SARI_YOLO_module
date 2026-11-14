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
