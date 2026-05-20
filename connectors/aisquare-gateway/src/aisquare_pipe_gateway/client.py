"""HTTP client for the AISquare Explainability gateway."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from aisquare.pipe.errors import ConfigValidationError

logger = logging.getLogger("aisquare.pipe.aisquare_gateway")

HEADER_API_KEY = "X-API-KEY"
HEADER_SOURCE_ID = "X-AISquare-Source-Id"
HEADER_CONTENT_TYPE = "X-AISquare-Content-Type"

DEFAULT_INGEST_PATH = "/v1/traces/ingest"
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5


@dataclass
class IngestResponse:
    """Result of a single ingest call."""

    status_code: int
    trace_id: str | None
    body: dict[str, Any] | None
    raw_text: str
    attempts: int


class GatewayClient:
    """Wraps the gateway HTTP API and the retry policy.

    Retries on 429 and 5xx with exponential backoff (0.5, 1.0, 2.0, ...).
    4xx responses other than 429 surface immediately as failures.
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        url = config.get("gateway_url")
        key = config.get("api_key")
        if not url or not isinstance(url, str):
            raise ConfigValidationError(
                "AISquare gateway config requires 'gateway_url'"
            )
        if not key or not isinstance(key, str):
            raise ConfigValidationError(
                "AISquare gateway config requires 'api_key'"
            )
        self._base_url = url.rstrip("/")
        self._api_key = key
        self._ingest_path = config.get("ingest_path", DEFAULT_INGEST_PATH)
        self._timeout = int(config.get("timeout_seconds", DEFAULT_TIMEOUT))
        self._max_retries = int(config.get("max_retries", DEFAULT_MAX_RETRIES))
        self._backoff_base = float(
            config.get("backoff_base_seconds", DEFAULT_BACKOFF_BASE_SECONDS)
        )
        self._sleep = sleep

    def _ingest_url(self) -> str:
        return f"{self._base_url}{self._ingest_path}"

    def ingest(
        self,
        payload: Any,
        *,
        source_id: str,
        content_type: str,
    ) -> IngestResponse:
        """POST one envelope payload. Returns the final response after retries.

        Raises requests.RequestException only after retries are exhausted on
        transport failures.
        """
        headers = {
            HEADER_API_KEY: self._api_key,
            HEADER_SOURCE_ID: source_id,
            HEADER_CONTENT_TYPE: content_type,
            "Content-Type": "application/json",
        }
        url = self._ingest_url()

        attempts = 0
        last_exc: Exception | None = None
        while attempts <= self._max_retries:
            attempts += 1
            try:
                resp = requests.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            except requests.RequestException as e:
                last_exc = e
                if attempts > self._max_retries:
                    raise
                self._sleep(self._backoff_for(attempts))
                continue

            if resp.status_code < 400:
                return _build_response(resp, attempts)

            if _is_retryable(resp.status_code) and attempts <= self._max_retries:
                logger.info(
                    "Gateway returned %d, retrying (attempt %d/%d)",
                    resp.status_code,
                    attempts,
                    self._max_retries,
                )
                self._sleep(self._backoff_for(attempts))
                continue

            return _build_response(resp, attempts)

        # Unreachable in practice — the loop always returns or raises.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("retry loop exited without a response")

    def health(self) -> bool:
        """Probe the gateway's /health endpoint."""
        try:
            resp = requests.get(
                f"{self._base_url}/health", timeout=self._timeout
            )
        except requests.RequestException as e:
            logger.warning("Gateway health probe failed: %s", e)
            return False
        return resp.status_code == 200

    def _backoff_for(self, attempt: int) -> float:
        return self._backoff_base * (2 ** (attempt - 1))


def _is_retryable(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _build_response(resp: requests.Response, attempts: int) -> IngestResponse:
    body: dict[str, Any] | None
    try:
        parsed = resp.json()
        body = parsed if isinstance(parsed, dict) else None
    except ValueError:
        body = None
    trace_id = body.get("trace_id") if isinstance(body, dict) else None
    return IngestResponse(
        status_code=resp.status_code,
        trace_id=trace_id,
        body=body,
        raw_text=resp.text,
        attempts=attempts,
    )
