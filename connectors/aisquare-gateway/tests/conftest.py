"""Mock AISquare gateway HTTP server fixture."""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

import pytest

API_KEY = "test-gateway-key"


class MockGateway:
    """Programmable mock gateway. Tests script the response queue."""

    def __init__(self) -> None:
        # Each entry: (status_code, body_dict_or_text)
        self.responses: deque[tuple[int, Any]] = deque()
        self.health_status = 200
        self.received: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def enqueue(self, status: int, body: Any = None) -> None:
        self.responses.append((status, body))

    def enqueue_many(self, statuses: list[int], final_body: Any = None) -> None:
        for s in statuses[:-1]:
            self.responses.append((s, None))
        self.responses.append((statuses[-1], final_body))

    def start(self) -> None:
        handler = _make_handler(self)
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _make_handler(state: MockGateway) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _write(self, status: int, body: Any) -> None:
            if isinstance(body, dict):
                raw = json.dumps(body).encode("utf-8")
                ctype = "application/json"
            elif isinstance(body, str):
                raw = body.encode("utf-8")
                ctype = "text/plain"
            else:
                raw = b""
                ctype = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._write(state.health_status, {"status": "ok"})
                return
            self._write(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length else b""
            try:
                parsed = json.loads(body_bytes) if body_bytes else None
            except ValueError:
                parsed = None
            state.received.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": parsed,
                }
            )

            if not state.responses:
                self._write(200, {"trace_id": f"trace-{len(state.received)}"})
                return
            status, body = state.responses.popleft()
            if body is None and 200 <= status < 300:
                body = {"trace_id": f"trace-{len(state.received)}"}
            self._write(status, body)

    return _Handler


@pytest.fixture
def mock_gateway() -> Callable[[], MockGateway]:
    servers: list[MockGateway] = []

    def factory() -> MockGateway:
        s = MockGateway()
        s.start()
        servers.append(s)
        return s

    yield factory

    for s in servers:
        s.stop()


@pytest.fixture
def gateway_config(mock_gateway) -> dict[str, Any]:
    server = mock_gateway()
    return {
        "_server": server,
        "gateway_url": server.url,
        "api_key": API_KEY,
        "max_retries": 2,
        "backoff_base_seconds": 0.0,  # keep tests instantaneous
        "timeout_seconds": 5,
    }
