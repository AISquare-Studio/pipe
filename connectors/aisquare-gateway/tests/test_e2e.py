"""End-to-end: N8nSource -> Pipeline -> AISquareGatewaySink.

Both halves run against their own in-process mock HTTP servers. The test
asserts that envelopes flow through unchanged — no shape mutation, every
n8n event lands at the gateway with the expected payload structure.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from aisquare.pipe import Pipeline
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_gateway.sink import AISquareGatewaySink
from aisquare_pipe_n8n.source import N8nSource

API_KEY_N8N = "n8n-key"
API_KEY_GW = "gw-key"


def _make_execution(eid: int) -> dict[str, Any]:
    return {
        "id": eid,
        "workflowId": "wf-X",
        "workflowData": {"name": "Workflow X"},
        "mode": "trigger",
        "finished": True,
        "status": "success",
        "startedAt": "2024-01-01T00:00:00Z",
        "stoppedAt": "2024-01-01T00:00:01Z",
        "data": {
            "resultData": {
                "runData": {
                    "Webhook": [
                        {
                            "startTime": 1,
                            "executionTime": 1,
                            "data": {"main": [[{"json": {"hit": True}}]]},
                            "source": [],
                        }
                    ],
                }
            }
        },
    }


class _N8nHandler(BaseHTTPRequestHandler):
    state: dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("X-N8N-API-KEY") != API_KEY_N8N:
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith("/api/v1/workflows"):
            self._json({"data": []})
            return
        if self.path.startswith("/api/v1/executions"):
            payload = {"data": list(reversed(self.state.get("executions", [])))}
            self._json(payload)
            return
        self.send_response(404)
        self.end_headers()

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _GatewayHandler(BaseHTTPRequestHandler):
    state: dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"error": "nope"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else None
        self.state.setdefault("received", []).append(
            {"headers": dict(self.headers), "body": body}
        )
        trace_id = f"trace-{len(self.state['received'])}"
        self._json(200, {"trace_id": trace_id})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def n8n_server():
    state: dict[str, Any] = {"executions": []}
    handler_cls = type("H", (_N8nHandler,), {"state": state})
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def gateway_server():
    state: dict[str, Any] = {}
    handler_cls = type("H", (_GatewayHandler,), {"state": state})
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_envelopes_flow_end_to_end(n8n_server, gateway_server, tmp_path):
    n8n_srv, n8n_state = n8n_server
    gw_srv, gw_state = gateway_server
    n8n_state["executions"] = [_make_execution(1), _make_execution(2)]

    config = {
        "n8n": {
            "n8n_url": f"http://127.0.0.1:{n8n_srv.server_address[1]}",
            "api_key": API_KEY_N8N,
            "poll_interval_seconds": 0,
            "cursor_path": str(tmp_path / "cursor.json"),
        },
        "aisquare-gateway": {
            "gateway_url": f"http://127.0.0.1:{gw_srv.server_address[1]}",
            "api_key": API_KEY_GW,
            "max_retries": 0,
            "backoff_base_seconds": 0.0,
        },
    }

    pipeline = Pipeline(source=N8nSource(), sink=AISquareGatewaySink())
    pull_params = PullParams(params={"max_polls": 1, "sleep": lambda _: None})
    result = pipeline.run(config, pull_params=pull_params)

    # Two executions × (1 start + 1 node_step + 1 complete) = 6 envelopes
    assert result.success_count == 6
    assert result.failure_count == 0

    received = gw_state.get("received", [])
    assert len(received) == 6

    # Headers preserve source_id and content_type
    for entry in received:
        assert entry["headers"]["X-AISquare-API-Key"] == API_KEY_GW
        assert entry["headers"]["X-AISquare-Source-Id"] == "n8n"
        assert (
            entry["headers"]["X-AISquare-Content-Type"]
            == "application/x-aisquare-trace+json"
        )

    # Event ordering across both executions: start, step, complete x2
    events = [e["body"]["data"]["event"] for e in received]
    assert events == [
        "workflow_start", "node_step", "workflow_complete",
        "workflow_start", "node_step", "workflow_complete",
    ]

    # Body shape was preserved (no mutation between source and sink)
    first = received[0]["body"]
    assert first["source_id"] == "n8n"
    assert first["content_type"] == "application/x-aisquare-trace+json"
    assert first["metadata"]["n8n_execution_id"] == "1"
    assert first["data"]["execution_id"] == "1"


def test_cursor_durable_across_pipeline_runs(n8n_server, gateway_server, tmp_path):
    n8n_srv, n8n_state = n8n_server
    gw_srv, gw_state = gateway_server
    n8n_state["executions"] = [_make_execution(1)]
    cursor_path = str(tmp_path / "cursor.json")

    config = {
        "n8n": {
            "n8n_url": f"http://127.0.0.1:{n8n_srv.server_address[1]}",
            "api_key": API_KEY_N8N,
            "poll_interval_seconds": 0,
            "cursor_path": cursor_path,
        },
        "aisquare-gateway": {
            "gateway_url": f"http://127.0.0.1:{gw_srv.server_address[1]}",
            "api_key": API_KEY_GW,
            "max_retries": 0,
            "backoff_base_seconds": 0.0,
        },
    }
    pull_params = PullParams(params={"max_polls": 1, "sleep": lambda _: None})

    first = Pipeline(source=N8nSource(), sink=AISquareGatewaySink()).run(
        config, pull_params=pull_params
    )
    assert first.success_count == 3  # start + node_step + complete

    # Second run, same executions on n8n's side: nothing new should be pushed.
    second = Pipeline(source=N8nSource(), sink=AISquareGatewaySink()).run(
        config, pull_params=pull_params
    )
    assert second.success_count == 0
    assert second.failure_count == 0

    # Add a new execution -> only it flows through.
    n8n_state["executions"].append(_make_execution(2))
    third = Pipeline(source=N8nSource(), sink=AISquareGatewaySink()).run(
        config, pull_params=pull_params
    )
    assert third.success_count == 3
    last_three = gw_state["received"][-3:]
    for entry in last_three:
        assert entry["body"]["metadata"]["n8n_execution_id"] == "2"
