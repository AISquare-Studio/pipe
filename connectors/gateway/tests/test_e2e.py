"""End-to-end: any TraceBatch-emitting source → Pipeline → AISquareGatewaySink.

This suite belongs to the gateway connector and therefore must not import
any other connector's package (connectors are independently-published
wheels). A small in-test mock source emits canned TraceBatch envelopes so
the sink can be exercised against a real in-process HTTP gateway without
coupling to the n8n connector's shaper.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from aisquare.pipe import Pipeline
from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_gateway.connector import AISquareGatewaySink

API_KEY_GW = "gw-key"
TRACE_CONTENT_TYPE = "application/x-aisquare-trace+json"


def _make_trace_batch(trace_id: str) -> dict[str, Any]:
    """A minimal but well-formed TraceBatch — `{trace_id, spans: [...]}`."""
    base_ns = 1_700_000_000_000_000_000
    return {
        "trace_id": trace_id,
        "spans": [
            {
                "span_id": f"{trace_id}-start",
                "trace_id": trace_id,
                "parent_span_id": None,
                "name": "workflow_start",
                "kind": "INTERNAL",
                "start_time": base_ns,
                "end_time": base_ns + 1_000_000,
                "attributes": {"sample.event": "workflow_start"},
                "status": {"code": "OK"},
            },
            {
                "span_id": f"{trace_id}-end",
                "trace_id": trace_id,
                "parent_span_id": f"{trace_id}-start",
                "name": "workflow_complete",
                "kind": "INTERNAL",
                "start_time": base_ns + 1_000_000,
                "end_time": base_ns + 2_000_000,
                "attributes": {"sample.event": "workflow_complete"},
                "status": {"code": "OK"},
            },
        ],
    }


class _CannedTraceSource(SourceConnector):
    """Emits the trace batches it's constructed with — no I/O, no other deps."""

    name = "canned-trace-source"
    version = "0.0.1"
    output_types = [TRACE_CONTENT_TYPE]
    auth_type = AuthType.NONE

    metadata_spec = {
        "idempotency_key": MetaField(
            type=str,
            required=True,
            description="Idempotency key the sink lifts onto X-Idempotency-Key.",
        ),
    }

    def __init__(self, batches: list[dict[str, Any]]) -> None:
        self._batches = batches

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        for batch in self._batches:
            trace_id = batch["trace_id"]
            yield DataEnvelope(
                content_type=TRACE_CONTENT_TYPE,
                data=batch,
                source_id="canned",
                metadata={
                    "trace_id": trace_id,
                    "idempotency_key": f"canned:final:{trace_id}",
                },
            )

    def validate_config(self, config: dict) -> bool:
        return True


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
            {"headers": dict(self.headers), "body": body, "path": self.path}
        )
        trace_id = body.get("trace_id") if isinstance(body, dict) else None
        self._json(
            200, {"trace_id": trace_id or f"trace-{len(self.state['received'])}"}
        )

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


def test_envelopes_flow_end_to_end(gateway_server):
    """Two canned TraceBatches land at the gateway as two ingest calls,
    each preserving body shape and the sink's header contract."""
    gw_srv, gw_state = gateway_server
    batches = [_make_trace_batch("canned-1"), _make_trace_batch("canned-2")]

    config = {
        "aisquare-gateway": {
            "gateway_url": f"http://127.0.0.1:{gw_srv.server_address[1]}",
            "api_key": API_KEY_GW,
            "max_retries": 0,
            "backoff_base_seconds": 0.0,
        },
    }

    pipeline = Pipeline(
        source=_CannedTraceSource(batches), sink=AISquareGatewaySink()
    )
    result = pipeline.run(config)

    assert result.success_count == 2
    assert result.failure_count == 0

    received = gw_state.get("received", [])
    assert len(received) == 2

    for entry, batch in zip(received, batches):
        normalised = {k.lower(): v for k, v in entry["headers"].items()}
        assert normalised["x-api-key"] == API_KEY_GW
        assert normalised["x-aisquare-source-id"] == "canned"
        assert (
            normalised["x-aisquare-content-type"]
            == "application/x-aisquare-trace+json"
        )
        assert normalised["x-idempotency-key"] == f"canned:final:{batch['trace_id']}"
        assert entry["body"]["trace_id"] == batch["trace_id"]
        assert isinstance(entry["body"]["spans"], list)
        assert len(entry["body"]["spans"]) == len(batch["spans"])
