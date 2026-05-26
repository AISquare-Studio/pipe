"""Salesforce SDK wrapper used by both source and sink connectors.

Centralises auth, retry-on-rate-limit, and error-mapping so the connector
classes can stay thin and declarative.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Iterator
from typing import Any

from simple_salesforce.exceptions import (
    SalesforceAuthenticationFailed,
    SalesforceError,
)

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_salesforce.auth import build_client
from aisquare_pipe_salesforce.constants import (
    DEFAULT_BATCH_SIZE,
    INITIAL_BACKOFF,
    MAX_RETRIES,
)

logger = logging.getLogger("aisquare.pipe.salesforce")


def _map_salesforce_errors(func):  # type: ignore[no-untyped-def]
    """Translate Salesforce SDK exceptions to framework errors."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except SalesforceAuthenticationFailed as e:
            raise ConfigValidationError(f"Salesforce auth failed: {e}") from e
        except SalesforceError as e:
            raise PipelineError(f"Salesforce API error: {e}") from e

    return wrapper


def _retry_on_rate_limit(func):  # type: ignore[no-untyped-def]
    """Retry with exponential backoff on REQUEST_LIMIT_EXCEEDED / HTTP 503."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except SalesforceError as e:
                code = getattr(e, "status", None)
                content = str(getattr(e, "content", "")) + str(e)
                rate_limited = code == 503 or "REQUEST_LIMIT_EXCEEDED" in content
                if not rate_limited or attempt == MAX_RETRIES - 1:
                    raise
                wait = INITIAL_BACKOFF * (2**attempt)
                logger.warning(
                    "Salesforce rate-limited (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
        raise PipelineError("Salesforce rate limit exceeded after max retries")

    return wrapper


class SalesforceClient:
    """Thin wrapper around simple_salesforce.Salesforce."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._sf = build_client(config)

    @_map_salesforce_errors
    def validate(self) -> bool:
        """Make a cheap call to confirm credentials work."""
        self._sf.limits()
        return True

    @_map_salesforce_errors
    @_retry_on_rate_limit
    def query_iter(
        self, soql: str, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> Iterator[dict[str, Any]]:
        """Stream records for a SOQL query, paginating transparently."""
        yield from self._sf.query_all_iter(soql, include_deleted=False, batch_size=batch_size)

    @_map_salesforce_errors
    @_retry_on_rate_limit
    def create(self, object_type: str, data: dict[str, Any]) -> dict[str, Any]:
        return getattr(self._sf, object_type).create(data)

    @_map_salesforce_errors
    @_retry_on_rate_limit
    def update(self, object_type: str, record_id: str, data: dict[str, Any]) -> int:
        return getattr(self._sf, object_type).update(record_id, data)

    @_map_salesforce_errors
    @_retry_on_rate_limit
    def upsert(
        self,
        object_type: str,
        external_field: str,
        external_value: str,
        data: dict[str, Any],
    ) -> int | dict[str, Any]:
        return getattr(self._sf, object_type).upsert(
            f"{external_field}/{external_value}", data
        )

    @_map_salesforce_errors
    def describe_sobjects(self) -> list[dict[str, Any]]:
        return self._sf.describe()["sobjects"]
