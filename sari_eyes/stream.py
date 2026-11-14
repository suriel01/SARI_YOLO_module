"""Live MJPEG / snapshot HTTP server for the operator UI and the brain.

Every route requires the shared stream token (header X-SARI-Stream-Token or query ?token=),
compared in constant time. No CORS headers are sent: browsers on other origins cannot read
the stream through scripts, while <img> tags with the token in the URL still work.
"""
from __future__ import annotations

import hmac
import html
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urlsplit

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-SARI-Stream-Token"
MJPEG_PATHS = {"/mjpeg", "/video_feed", "/stream"}
FRAME_INTERVAL_S = 0.04  # ~25 FPS


class FrameBuffer:
    """Latest JPEG frame, shared between the inference loop and the HTTP server."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jpeg: Optional[bytes] = None

    def set(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg = jpeg

    def get(self) -> Optional[bytes]:
        with self._lock:
            return self._jpeg


def token_ok(supplied: Optional[str], expected: str) -> bool:
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _make_handler(buffer: FrameBuffer, token: str, placeholder: Callable[[], bytes]):
    class StreamHandler(BaseHTTPRequestHandler):
        server_version = "SARI-Eyes"
        sys_version = ""

        def log_message(self, fmt, *args):  # the query string may carry the token: never log it
            logger.debug("stream request %s from %s", urlsplit(self.path).path, self.client_address[0])

        def _frame(self) -> bytes:
            return buffer.get() or placeholder()

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = urlsplit(self.path)
            supplied = self.headers.get(TOKEN_HEADER) or (parse_qs(parts.query).get("token") or [None])[0]
            if not token_ok(supplied, token):
                logger.warning("stream request rejected (bad or missing token) from %s", self.client_address[0])
                self._send(401, "text/plain; charset=utf-8", b"unauthorized\n")
                return

            if parts.path in MJPEG_PATHS:
                self._stream_mjpeg()
            elif parts.path == "/snapshot":
                self._send(200, "image/jpeg", self._frame())
            elif parts.path == "/":
                src = html.escape(f"/mjpeg?token={quote(supplied)}")
                page = f"<!DOCTYPE html><html><body style='background:#0f172a;color:#fff;text-align:center'>" \
                       f"<h2>SARI Eyes</h2><img src=\"{src}\" style='max-width:960px;width:100%'/></body></html>"
                self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def _stream_mjpeg(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + self._frame() + b"\r\n")
                    self.wfile.flush()
                    time.sleep(FRAME_INTERVAL_S)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return StreamHandler


def start_stream_server(
    buffer: FrameBuffer, *, token: str, bind: str, port: int, placeholder: Callable[[], bytes]
) -> ThreadingHTTPServer:
    """Start the server on a daemon thread and return it (server_address holds the bound port)."""
    server = ThreadingHTTPServer((bind, port), _make_handler(buffer, token, placeholder))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="MJPEGStreamThread").start()
    logger.info("Authenticated MJPEG server listening on %s:%s", bind, server.server_address[1])
    return server
