"""Authenticated MJPEG/snapshot server (single native implementation)."""
import http.client
from unittest import mock

import pytest

from conftest import ROOT, base_env, project_code
from sari_eyes import stream
from sari_eyes.config import ConfigError, load_settings

TOKEN = "test-stream-token-0123456789"


@pytest.fixture
def server():
    buffer = stream.FrameBuffer()
    buffer.set(b"\xff\xd8JPEGDATA\xff\xd9")
    srv = stream.start_stream_server(buffer, token=TOKEN, bind="127.0.0.1", port=0,
                                     placeholder=lambda: b"\xff\xd8WAIT\xff\xd9")
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(srv, path, headers=None, read=True):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read() if read else resp.read(64)
    conn.close()
    return resp, body


@pytest.mark.parametrize("path", ["/snapshot", "/mjpeg", "/video_feed", "/stream", "/"])
def test_missing_token_is_rejected(server, path):
    resp, _ = _get(server, path)
    assert resp.status == 401
    assert resp.getheader("Access-Control-Allow-Origin") is None


@pytest.mark.parametrize("path", ["/snapshot?token=wrong", "/mjpeg?token="])
def test_wrong_token_is_rejected(server, path):
    assert _get(server, path)[0].status == 401


def test_snapshot_with_header_token(server):
    resp, body = _get(server, "/snapshot", {stream.TOKEN_HEADER: TOKEN})
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "image/jpeg"
    assert body == b"\xff\xd8JPEGDATA\xff\xd9"
    assert resp.getheader("Access-Control-Allow-Origin") is None


def test_snapshot_with_query_token(server):
    resp, body = _get(server, f"/snapshot?token={TOKEN}")
    assert resp.status == 200 and body.startswith(b"\xff\xd8")


def test_mjpeg_with_token_streams_frames(server):
    resp, chunk = _get(server, f"/mjpeg?token={TOKEN}", read=False)
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("multipart/x-mixed-replace")
    assert b"--frame" in chunk


def test_unknown_path_is_404_even_with_token(server):
    assert _get(server, "/admin", {stream.TOKEN_HEADER: TOKEN})[0].status == 404


def test_placeholder_served_before_first_frame():
    buffer = stream.FrameBuffer()
    srv = stream.start_stream_server(buffer, token=TOKEN, bind="127.0.0.1", port=0,
                                     placeholder=lambda: b"\xff\xd8WAIT\xff\xd9")
    try:
        assert _get(srv, f"/snapshot?token={TOKEN}")[1] == b"\xff\xd8WAIT\xff\xd9"
    finally:
        srv.shutdown()
        srv.server_close()


def test_token_comparison_is_constant_time():
    with mock.patch("sari_eyes.stream.hmac.compare_digest", return_value=True) as cmp:
        assert stream.token_ok("abc", "abc") is True
    cmp.assert_called_once()
    assert stream.token_ok(None, TOKEN) is False
    assert stream.token_ok(TOKEN, "") is False


# --- config ---

def test_stream_token_required_and_bind_configurable():
    s = load_settings(base_env())
    assert s.stream_token == TOKEN and s.stream_bind == "0.0.0.0" and s.stream_port == 8080
    assert load_settings(base_env(STREAM_BIND="127.0.0.1")).stream_bind == "127.0.0.1"


def test_short_stream_token_is_rejected():
    with pytest.raises(ConfigError):
        load_settings(base_env(EYES_STREAM_TOKEN="short"))


# --- static guards ---

def test_single_server_implementation_without_wildcard_cors():
    code = project_code()
    assert "Access-Control-Allow-Origin" not in code
    assert "flask" not in code.lower()
