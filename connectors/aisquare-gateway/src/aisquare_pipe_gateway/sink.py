"""AISquare Explainability gateway sink connector."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PushParams, PushResult

from aisquare_pipe_gateway.client import (
    DEFAULT_INGEST_PATH,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    GatewayClient,
)

logger = logging.getLogger("aisquare.pipe.aisquare_gateway")

TRACE_CONTENT_TYPE = "application/x-aisquare-trace+json"


class AISquareGatewaySink(SinkConnector):
    """Pushes envelopes to the AISquare Explainability gateway.

    Accepts the canonical trace content type. The envelope's ``data``
    (expected to be a JSON-compatible dict) is POSTed to the ingest
    endpoint. ``source_id`` and ``content_type`` ride along as headers
    so the gateway can route by source without inspecting the body.
    """

    name = "aisquare-gateway"
    version = "0.1.0"
    input_types = [TRACE_CONTENT_TYPE]
    auth_type = AuthType.API_KEY

    description = "Pushes envelopes to the AISquare Explainability gateway."
    docs_url = ""

    CONFIG_SPEC: dict[str, MetaField] = {
        "gateway_url": MetaField(
            type=str, required=True, description="Base URL of the AISquare gateway"
        ),
        "api_key": MetaField(
            type=str, required=True, description="AISquare gateway API key"
        ),
        "ingest_path": MetaField(
            type=str,
            required=False,
            default=DEFAULT_INGEST_PATH,
            description="Trace ingest endpoint path",
        ),
        "timeout_seconds": MetaField(
            type=int,
            required=False,
            default=DEFAULT_TIMEOUT,
            description="Request timeout",
        ),
        "max_retries": MetaField(
            type=int,
            required=False,
            default=DEFAULT_MAX_RETRIES,
            description="Retries on 429/5xx before giving up",
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        try:
            client = GatewayClient(config)
        except Exception as e:
            return PushResult(success=False, error=str(e))

        try:
            response = client.ingest(
                _payload_for(envelope),
                source_id=envelope.source_id,
                content_type=envelope.content_type,
            )
        except Exception as e:
            logger.error("Gateway push transport error: %s", e)
            return PushResult(
                success=False, error=f"Transport error: {e}"
            )

        if 200 <= response.status_code < 300:
            return PushResult(
                success=True,
                ref=response.trace_id,
                metadata={
                    "http_status": response.status_code,
                    "attempts": response.attempts,
                    "response": response.body,
                },
            )

        error = (
            f"Gateway returned HTTP {response.status_code} after "
            f"{response.attempts} attempt(s): {response.raw_text[:200]}"
        )
        logger.warning(error)
        return PushResult(
            success=False,
            error=error,
            metadata={
                "http_status": response.status_code,
                "attempts": response.attempts,
            },
        )

    def validate_config(self, config: dict) -> bool:
        if not config.get("gateway_url") or not config.get("api_key"):
            return False
        try:
            return GatewayClient(config).health()
        except Exception as e:
            logger.warning("validate_config failed: %s", e)
            return False


def _payload_for(envelope: DataEnvelope) -> Any:
    """Build the JSON body the gateway expects.

    The gateway accepts an OTel-shaped TraceBatch (`trace_id` + `spans[]`).
    We convert each n8n trace envelope into a single-span TraceBatch.
    Spans within the same n8n execution share a trace_id so the gateway
    can aggregate them server-side.
    """
    data = envelope.data if isinstance(envelope.data, dict) else {}
    metadata = envelope.metadata or {}

    execution_id = str(
        metadata.get("n8n_execution_id") or data.get("execution_id") or "unknown"
    )
    workflow_id = str(
        metadata.get("n8n_workflow_id") or data.get("workflow_id") or "unknown"
    )
    event = str(
        metadata.get("n8n_event") or data.get("event") or "unknown"
    )

    trace_id = _safe_id(f"n8n-{workflow_id}-{execution_id}")

    if event == "workflow_start":
        span_id = _safe_id(f"{trace_id}-start")
        span_name = "n8n.workflow.start"
        parent_span_id: str | None = None
    elif event == "workflow_complete":
        span_id = _safe_id(f"{trace_id}-complete")
        span_name = "n8n.workflow.complete"
        parent_span_id = _safe_id(f"{trace_id}-start")
    elif event == "node_step":
        node_name = str(data.get("node_name") or "unknown")
        run_index = data.get("run_index", 0)
        span_id = _safe_id(f"{trace_id}-node-{node_name}-{run_index}")
        span_name = f"n8n.node.{node_name}"[:1000]
        parent_span_id = _safe_id(f"{trace_id}-start")
    else:
        span_id = _safe_id(f"{trace_id}-{event}")
        span_name = f"n8n.{event}"[:1000]
        parent_span_id = None

    attributes: dict[str, Any] = {
        "n8n.event": event,
        "n8n.execution_id": execution_id,
        "n8n.workflow_id": workflow_id,
    }
    for key, value in data.items():
        if key in {"event", "execution_id", "workflow_id"}:
            continue
        attributes[f"n8n.{key}"] = _attr_safe(value)

    span: dict[str, Any] = {
        "trace_id": trace_id,
        "span_id": span_id,
        "name": span_name,
        "kind": "INTERNAL",
        "attributes": attributes,
    }
    if parent_span_id:
        span["parent_span_id"] = parent_span_id

    return {"trace_id": trace_id, "spans": [span]}


def _safe_id(value: str, length: int = 128) -> str:
    """Keep IDs readable when possible, hash-shorten when too long."""
    if len(value) <= length:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    head = value[: length - 17]
    return f"{head}:{digest}"


def _attr_safe(value: Any) -> Any:
    """Coerce attribute values into JSON/OTel-safe primitives."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        import json

        return json.dumps(value, default=str)[:4000]
    except Exception:
        return str(value)[:4000]
