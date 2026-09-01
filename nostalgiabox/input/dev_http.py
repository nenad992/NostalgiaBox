"""Loopback HTTP remote for laptop / Tilt development.

Binds 127.0.0.1 only. Off unless ``input.dev_http`` is enabled, so the Pi
remote path (evdev / CEC) is unchanged.
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urlparse

from ..actions import Action, InputEvent
from .base import InputBackend

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765

_ROUTES = {
    "/channel/up": InputEvent(Action.CHANNEL_UP),
    "/channel/down": InputEvent(Action.CHANNEL_DOWN),
    "/volume/up": InputEvent(Action.VOLUME_UP),
    "/volume/down": InputEvent(Action.VOLUME_DOWN),
    "/mute": InputEvent(Action.MUTE),
    "/info": InputEvent(Action.INFO),
    "/last": InputEvent(Action.LAST_CHANNEL),
    "/power": InputEvent(Action.POWER),
    "/quit": InputEvent(Action.QUIT),
}


def path_to_event(path: str) -> Optional[InputEvent]:
    """Map an HTTP path to an :class:`InputEvent`, or None if unknown."""
    normalized = path.split("?", 1)[0].rstrip("/") or "/"
    if normalized in _ROUTES:
        return _ROUTES[normalized]
    prefix = "/digit/"
    if normalized.startswith(prefix):
        rest = normalized[len(prefix) :]
        if len(rest) == 1 and rest.isdigit():
            return InputEvent.digit(int(rest))
    return None


def _is_health(path: str) -> bool:
    normalized = path.split("?", 1)[0].rstrip("/") or "/"
    return normalized == "/health"


class DevHttpBackend(InputBackend):
    """Serve a tiny control API on loopback and enqueue matching actions."""

    name = "dev-http"

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        super().__init__()
        self._requested_port = port
        self.port = port
        self._httpd: HTTPServer | None = None

    def start(self, queue) -> None:  # type: ignore[override]
        handler = _handler_for(self)
        try:
            self._httpd = HTTPServer(("127.0.0.1", self._requested_port), handler)
        except OSError as exc:
            raise RuntimeError(
                f"dev HTTP control address 127.0.0.1:{self._requested_port} "
                f"is already in use ({exc})"
            ) from exc
        self.port = int(self._httpd.server_address[1])
        log.info("dev HTTP control listening on http://127.0.0.1:%s", self.port)
        super().start(queue)

    def _run(self) -> None:
        assert self._httpd is not None
        self._httpd.serve_forever(poll_interval=0.2)

    def _close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _handler_for(backend: DevHttpBackend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            log.debug("dev-http: " + fmt, *args)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if _is_health(path):
                self._send(200, b"ok")
                return
            self._send(405 if path_to_event(path) else 404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if _is_health(path):
                self._send(405)
                return
            event = path_to_event(path)
            if event is None:
                self._send(404)
                return
            backend.emit(event)
            self._send(204)

        def _send(self, code: int, body: bytes = b"") -> None:
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)

    return Handler


__all__ = ["DevHttpBackend", "path_to_event", "DEFAULT_PORT"]
