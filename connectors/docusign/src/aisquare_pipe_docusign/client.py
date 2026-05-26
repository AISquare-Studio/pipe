"""DocuSign SDK wrapper used by both source and sink connectors."""

from __future__ import annotations

import functools
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

from docusign_esign import EnvelopesApi, FoldersApi
from docusign_esign.client.api_exception import ApiException

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_docusign.auth import build_client
from aisquare_pipe_docusign.constants import (
    DEFAULT_PAGE_SIZE,
    INITIAL_BACKOFF,
    MAX_RETRIES,
)

logger = logging.getLogger("aisquare.pipe.docusign")


def _map_docusign_errors(func):  # type: ignore[no-untyped-def]
    """Translate DocuSign SDK exceptions to framework errors."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ApiException as e:
            status = getattr(e, "status", None)
            if status == 401:
                raise ConfigValidationError(f"DocuSign auth failed: {e}") from e
            raise PipelineError(f"DocuSign API error ({status}): {e}") from e

    return wrapper


def _retry_on_rate_limit(func):  # type: ignore[no-untyped-def]
    """Retry with exponential backoff on HTTP 429 / 503."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except ApiException as e:
                status = getattr(e, "status", None)
                if status not in (429, 503) or attempt == MAX_RETRIES - 1:
                    raise
                wait = INITIAL_BACKOFF * (2**attempt)
                logger.warning(
                    "DocuSign rate-limited (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
        raise PipelineError("DocuSign rate limit exceeded after max retries")

    return wrapper


class DocusignClient:
    """Wraps docusign-esign with auth, pagination, retries, and error mapping."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_client, self.account_id = build_client(config)
        self._envelopes = EnvelopesApi(self._api_client)
        self._folders = FoldersApi(self._api_client)

    @_map_docusign_errors
    def validate(self) -> bool:
        """Cheap call to confirm credentials work."""
        self._envelopes.list_status_changes(
            self.account_id, count="1", from_date="2024-01-01"
        )
        return True

    @_map_docusign_errors
    @_retry_on_rate_limit
    def list_envelopes(
        self,
        status: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        folder_id: str | None = None,
        envelope_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> Iterator[Any]:
        """Stream envelopes matching the filter criteria, paginating transparently."""
        kwargs: dict[str, Any] = {}
        if status:
            kwargs["status"] = status
        if from_date:
            kwargs["from_date"] = from_date
        if to_date:
            kwargs["to_date"] = to_date
        if folder_id:
            kwargs["folder_ids"] = folder_id
        if envelope_ids:
            kwargs["envelope_ids"] = ",".join(envelope_ids)

        yielded = 0
        start = 0
        while True:
            page = self._envelopes.list_status_changes(
                self.account_id,
                start_position=str(start),
                count=str(DEFAULT_PAGE_SIZE),
                **kwargs,
            )
            envelopes = getattr(page, "envelopes", None) or []
            for env in envelopes:
                if limit is not None and yielded >= limit:
                    return
                yield env
                yielded += 1
            try:
                next_uri = page.next_uri
            except AttributeError:
                next_uri = None
            if not next_uri or not envelopes:
                return
            start += len(envelopes)

    @_map_docusign_errors
    @_retry_on_rate_limit
    def get_envelope(self, envelope_id: str) -> Any:
        return self._envelopes.get_envelope(self.account_id, envelope_id)

    @_map_docusign_errors
    @_retry_on_rate_limit
    def list_documents(self, envelope_id: str) -> list[Any]:
        result = self._envelopes.list_documents(self.account_id, envelope_id)
        return getattr(result, "envelope_documents", None) or []

    @_map_docusign_errors
    @_retry_on_rate_limit
    def get_document_bytes(self, envelope_id: str, document_id: str) -> bytes:
        """Download a document and return its bytes. Cleans up the temp file."""
        temp_path = self._envelopes.get_document(
            self.account_id, document_id, envelope_id
        )
        try:
            with open(temp_path, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                logger.debug("Failed to clean up temp file %s", temp_path)

    @_map_docusign_errors
    @_retry_on_rate_limit
    def create_envelope(self, envelope_definition: Any) -> Any:
        return self._envelopes.create_envelope(
            self.account_id, envelope_definition=envelope_definition
        )

    @_map_docusign_errors
    def list_folders(self) -> list[Any]:
        result = self._folders.list(self.account_id)
        return getattr(result, "folders", None) or []
