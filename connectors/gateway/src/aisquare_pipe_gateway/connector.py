"""AISquare Explainability gateway sink connector."""

from __future__ import annotations

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

    metadata_spec = {
        "idempotency_key": MetaField(
            type=str,
            required=True,
            description=(
                "Stable key forwarded as the X-Idempotency-Key header so the "
                "gateway can dedupe retries and steady-state re-emissions. "
                "Required so the framework warns when a paired source doesn't "
                "produce it."
            ),
        ),
    }

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
                idempotency_key=(envelope.metadata or {}).get("idempotency_key"),
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
    """Forward the envelope's ``data`` straight through as the JSON body.

    Sources targeting this sink are expected to put a fully-shaped
    ``{trace_id, spans: [...]}`` TraceBatch dict in ``envelope.data``. The
    sink itself stays stateless and source-agnostic — see e.g.
    ``aisquare_pipe_n8n.spans.execution_to_trace_batch`` for the canonical
    n8n shaper.
    """
    if not isinstance(envelope.data, dict):
        raise ValueError(
            "aisquare-gateway sink expects envelope.data to be a TraceBatch "
            f"dict, got {type(envelope.data).__name__}"
        )
    if "trace_id" not in envelope.data or "spans" not in envelope.data:
        raise ValueError(
            "aisquare-gateway sink expects envelope.data to contain "
            "'trace_id' and 'spans' keys"
        )
    return envelope.data
