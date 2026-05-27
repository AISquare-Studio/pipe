"""Test helpers for the gateway sink."""

from __future__ import annotations

from aisquare.pipe.core.envelope import DataEnvelope

TRACE_CONTENT_TYPE = "application/x-aisquare-trace+json"


def make_trace_envelope(
    *,
    event: str = "final",
    trace_id: str = "n8n-wf-1-1",
    execution_id: str = "1",
    workflow_id: str = "wf-1",
    workflow_name: str = "Demo",
    spans: list[dict] | None = None,
    idempotency_key: str | None = None,
) -> DataEnvelope:
    """Build a TraceBatch-shaped envelope for sink tests.

    Mirrors the shape that ``aisquare_pipe_n8n.spans.execution_to_trace_batch``
    produces — a dict with ``trace_id`` and ``spans``, where each span has the
    OTel-shaped keys the gateway expects.
    """
    if spans is None:
        spans = [
            {
                "span_id": f"{trace_id}-start",
                "trace_id": trace_id,
                "parent_span_id": None,
                "name": workflow_name,
                "kind": "INTERNAL",
                "start_time": 1_700_000_000_000_000_000,
                "end_time": 1_700_000_001_000_000_000,
                "attributes": {
                    "n8n.event": "workflow_start",
                    "n8n.execution_id": execution_id,
                    "n8n.workflow_id": workflow_id,
                    "agent.name": workflow_name,
                    "openinference.span.kind": "AGENT",
                },
                "status": {"code": "OK"},
            }
        ]

    data = {"trace_id": trace_id, "spans": spans}
    meta = {
        "n8n_execution_id": execution_id,
        "n8n_workflow_id": workflow_id,
        "n8n_workflow_name": workflow_name,
        "n8n_event": event,
        "trace_id": trace_id,
    }
    if idempotency_key:
        meta["idempotency_key"] = idempotency_key
    return DataEnvelope(
        content_type=TRACE_CONTENT_TYPE,
        data=data,
        source_id="n8n",
        metadata=meta,
    )
