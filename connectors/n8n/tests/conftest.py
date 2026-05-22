"""Shared fixtures: an in-process mock n8n HTTP server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import pytest

API_KEY = "test-key"


class MockN8n:
    """Programmable mock n8n server.

    Tests set ``server.executions`` (full list, any order — handler sorts
    descending to mimic n8n) and ``server.workflow_defs`` (keyed by
    workflow id, returned by ``GET /api/v1/workflows/{id}``).

    Handled endpoints:
      * ``GET /api/v1/executions`` — supports ``workflowId``, ``limit``,
        ``includeData``, ``status=running`` query params.
      * ``GET /api/v1/workflows`` — used by ``N8nClient.validate``.
      * ``GET /api/v1/workflows/{id}`` — used to enrich in-progress stubs.
    """

    def __init__(self) -> None:
        self.executions: list[dict[str, Any]] = []
        self.workflows: list[dict[str, Any]] = [{"id": "wf-1", "name": "Demo"}]
        self.workflow_defs: dict[str, dict[str, Any]] = {}
        self.require_auth = True
        self.requests: list[tuple[str, dict[str, list[str]], dict[str, str]]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

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


def _make_handler(state: MockN8n) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            if not state.require_auth:
                return True
            return self.headers.get("X-N8N-API-KEY") == API_KEY

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            headers = {k: v for k, v in self.headers.items()}
            state.requests.append((parsed.path, query, headers))

            if not self._check_auth():
                self._write_json(401, {"error": "unauthorized"})
                return

            if parsed.path == "/api/v1/workflows":
                limit = int(query.get("limit", ["100"])[0])
                self._write_json(200, {"data": state.workflows[:limit]})
                return

            if parsed.path.startswith("/api/v1/workflows/"):
                wid = parsed.path.rsplit("/", 1)[1]
                if wid in state.workflow_defs:
                    self._write_json(200, state.workflow_defs[wid])
                else:
                    self._write_json(404, {"error": "workflow not found"})
                return

            if parsed.path == "/api/v1/executions":
                workflow_id = query.get("workflowId", [None])[0]
                limit = int(query.get("limit", ["100"])[0])
                status_filter = query.get("status", [None])[0]

                filtered = list(state.executions)
                if status_filter == "running":
                    filtered = [e for e in filtered if not e.get("finished")]
                if workflow_id:
                    filtered = [
                        e for e in filtered if str(e.get("workflowId")) == workflow_id
                    ]
                # n8n returns most-recent-first; mimic that.
                filtered = sorted(filtered, key=lambda e: int(e["id"]), reverse=True)
                self._write_json(200, {"data": filtered[:limit]})
                return

            self._write_json(404, {"error": "not found"})

    return _Handler


@pytest.fixture
def mock_n8n() -> Callable[[], MockN8n]:
    """Yield a started MockN8n; stops it on teardown."""
    servers: list[MockN8n] = []

    def factory() -> MockN8n:
        s = MockN8n()
        s.start()
        servers.append(s)
        return s

    yield factory

    for s in servers:
        s.stop()


@pytest.fixture
def n8n_config(mock_n8n, tmp_path) -> dict[str, Any]:
    """A connector config pointing at a fresh mock server."""
    server = mock_n8n()
    return {
        "_server": server,  # available to tests that need to mutate state
        "n8n_url": server.url,
        "api_key": API_KEY,
        "poll_interval_seconds": 0,
        "cursor_path": str(tmp_path / "cursor.json"),
    }
