"""Test helpers for the gateway sink."""

from __future__ import annotations

from aisquare.pipe.core.envelope import DataEnvelope

TRACE_CONTENT_TYPE = "application/x-aisquare-trace+json"


def make_trace_envelope(
    *,
    event: str = "node_step",
    execution_id: str = "1",
    workflow_id: str = "wf-1",
    workflow_name: str = "Demo",
    node_name: str | None = None,
    extra_data: dict | None = None,
    extra_meta: dict | None = None,
) -> DataEnvelope:
    data = {
        "event": event,
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
    }
    if node_name is not None:
        data["node_name"] = node_name
    if extra_data:
        data.update(extra_data)
    meta = {
        "n8n_execution_id": execution_id,
        "n8n_workflow_id": workflow_id,
        "n8n_workflow_name": workflow_name,
        "n8n_event": event,
    }
    if extra_meta:
        meta.update(extra_meta)
    return DataEnvelope(
        content_type=TRACE_CONTENT_TYPE,
        data=data,
        source_id="n8n",
        metadata=meta,
    )
