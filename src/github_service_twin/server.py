"""HTTP server exposing the GitHub Digital Twin REST API v3 and control surface."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .api_handlers import TwinService
from .models import ROLE_CODING_AGENT, VALID_ROLES, ActorIdentity


def resolve_actor_from_headers(headers: Any) -> ActorIdentity:
    """Resolve opaque actor identity without ever logging or persisting credentials."""
    login = str(headers.get("X-Twin-Actor") or "twin-coding-agent").strip()
    role = str(headers.get("X-Twin-Role") or ROLE_CODING_AGENT).strip()
    if role not in VALID_ROLES:
        role = ROLE_CODING_AGENT
    return ActorIdentity(login=login or "twin-coding-agent", role=role)


class TwinHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], service: TwinService) -> None:
        self.service = service
        super().__init__(server_address, _TwinRequestHandler)


class _TwinRequestHandler(BaseHTTPRequestHandler):
    server: TwinHTTPServer  # type: ignore[assignment]

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def _handle(self, method: str) -> None:
        try:
            body = self._read_json_body() if method in {"POST", "PUT"} else {}
        except json.JSONDecodeError:
            self._write_json(400, {"message": "Invalid JSON body"}, {})
            return
        parts = urlsplit(self.path)
        actor = resolve_actor_from_headers(self.headers)
        status, payload, headers = self.server.service.dispatch(
            method, parts.path, parts.query, body, actor
        )
        self._write_json(status, payload, headers)

    def _write_json(self, status: int, payload: Any, headers: dict[str, str]) -> None:
        encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_background_server(service: TwinService, host: str = "127.0.0.1", port: int = 0) -> tuple[TwinHTTPServer, threading.Thread, str]:
    httpd = TwinHTTPServer((host, port), service)
    actual_port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://{host}:{actual_port}"
