"""GleanSource: Glean documents as text envelopes, from a saved query or ids.

``config`` is the tenant's credential blob (:mod:`.credentials`). Pull params
pick the documents, never the credential:

* ``query`` (a saved query, paged through with Glean's cursor) or
  ``document_ids`` (a list), exactly one;
* ``datasources`` (optional, with ``query``): only these Glean datasources;
* ``max_documents`` (default 100, at most 1000).

Each readable document becomes one ``text/plain`` envelope whose ``data`` is
the document's full text, with ``metadata`` ``glean_document_id``, ``title``,
``url``, ``datasource``, ``mime_type``, ``create_time``, ``author`` and a
version marker. A caller keeps the marker and re-reads the document when it
changes:

* ``update_time``: Glean's ``metadata.updateTime``, when Glean sends one;
* otherwise ``content_sha256``: the hex SHA-256 of ``data`` (UTF-8). Such
  documents are counted in one INFO line.

Every envelope carries exactly one of the two.

A param naming a credential key (``host``, ``token``, ``token_kind``,
``act_as``, ``allowed_hosts``) is refused. Documents Glean cannot read for
this user are skipped and counted in one log line, with no ids or text. A
search that reaches the page cap while Glean still has results logs one
WARNING with counts only.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_glean.client import GleanAPIError, GleanClient
from aisquare_pipe_glean.credentials import (
    SERVER_KEYS,
    GleanCredentials,
    GleanNotConfigured,
    load_credentials,
)
from aisquare_pipe_glean.documents import document_record, search_hit, split_documents
from aisquare_pipe_glean.toolkit import GleanRequestError, source_error

logger = logging.getLogger("aisquare.pipe.glean")

DEFAULT_MAX_DOCUMENTS = 100
MAX_DOCUMENTS = 1000
SEARCH_PAGE_SIZE = 50
#: Documents per getdocuments call.
READ_BATCH = 20
#: Search pages followed at most, whatever the cursor says.
MAX_SEARCH_PAGES = 50


def _options(params: PullParams | dict[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    raw = params.params if isinstance(params, PullParams) else params
    if not isinstance(raw, dict):
        raise ValueError("pull params must be a mapping")  # noqa: TRY004 — a host answers TypeError 500
    server = sorted(SERVER_KEYS.intersection(raw))
    if server:
        raise ValueError(
            f"pull param {server[0]!r} is server-controlled and cannot be set by a caller"
        )
    unknown = sorted(
        set(raw) - {"query", "document_ids", "datasources", "max_documents"}
    )
    if unknown:
        raise ValueError(f"unknown pull param {unknown[0]!r}")
    return raw


class GleanSource(SourceConnector):
    """Pulls Glean documents' full text, with their update time."""

    name = "glean-source"
    version = "0.1.0"
    # Framework class attributes, declared plain on SourceConnector.
    output_types = ["text/plain"]  # noqa: RUF012
    auth_type = AuthType.API_KEY
    description = "Pulls Glean documents (a saved query or document ids) as text with their update time."
    docs_url = "https://developers.glean.com/api/client-api/documents/getdocuments"

    CONFIG_SPEC: dict[str, MetaField] = {  # noqa: RUF012
        "host": MetaField(
            type=str,
            required=True,
            description="<instance>-be.glean.com (server-side only)",
        ),
        "token": MetaField(
            type=str,
            required=True,
            description="Glean Client API token (server-side only)",
        ),
        "token_kind": MetaField(
            type=str,
            required=False,
            default="user",
            description="'user' (default) or 'global'",
        ),
        "act_as": MetaField(
            type=str,
            required=False,
            description="With a global token: the email Glean reads as",
        ),
        "allowed_hosts": MetaField(
            type=list,
            required=False,
            description="Hosts allowed besides <instance>-be.glean.com",
        ),
    }

    metadata_spec = {  # noqa: RUF012
        "glean_document_id": MetaField(
            type=str, required=True, description="Glean document id"
        ),
        "title": MetaField(type=str, required=False, description="Document title"),
        "url": MetaField(type=str, required=False, description="Document URL"),
        "datasource": MetaField(
            type=str, required=False, description="Glean datasource"
        ),
        "mime_type": MetaField(
            type=str, required=False, description="Mime type Glean reports"
        ),
        "create_time": MetaField(
            type=str, required=False, description="RFC 3339 creation time"
        ),
        "update_time": MetaField(
            type=str,
            required=False,
            description="RFC 3339 last update: the version marker",
        ),
        "content_sha256": MetaField(
            type=str,
            required=False,
            description=(
                "Hex SHA-256 of the text: the version marker when Glean sends "
                "no updateTime"
            ),
        ),
        "author": MetaField(type=str, required=False, description="Author's name"),
    }

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def validate_config(self, config: dict) -> bool:
        try:
            load_credentials(config)
        except GleanNotConfigured:
            return False
        return True

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield one envelope per readable document.

        Raises, as pipe-as-service answers them:

        * 422, a ``ValueError``: :class:`GleanNotConfigured` without a usable
          credential (before any request); bad params; and what Glean
          refused that the caller can fix
          (:class:`~aisquare_pipe_glean.toolkit.GleanRequestError`);
        * 502, a :class:`~aisquare.pipe.errors.PipelineError`: any other
          Glean failure (a refused token or scope, throttling, a timeout, a
          5xx), with ``retry_after`` when Glean gave one.

        Neither carries Glean's own error text."""
        creds = load_credentials(config)
        try:
            yield from self._pull(creds, params)
        except GleanAPIError as e:
            raise source_error(e) from None

    def _pull(
        self, creds: GleanCredentials, params: PullParams | None
    ) -> Iterator[DataEnvelope]:
        options = _options(params)
        query = options.get("query")
        ids = options.get("document_ids")
        if (query is None) == (ids is None):
            raise ValueError("give exactly one of query and document_ids")
        limit = options.get("max_documents", DEFAULT_MAX_DOCUMENTS)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_DOCUMENTS
        ):
            raise ValueError(
                f"max_documents must be an integer from 1 to {MAX_DOCUMENTS}"
            )
        datasources = options.get("datasources") or []
        if not isinstance(datasources, list) or not all(
            isinstance(d, str) and d for d in datasources
        ):
            raise ValueError("datasources must be a list of datasource names")
        if ids is not None:
            if datasources:
                raise ValueError("datasources applies to query, not document_ids")
            if not isinstance(ids, list) or not all(
                isinstance(i, str) and i.strip() for i in ids
            ):
                raise ValueError("document_ids must be a list of document ids")
        elif not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")

        with GleanClient(creds, session=self._session) as client:
            if isinstance(ids, list):
                wanted = list(dict.fromkeys(i.strip() for i in ids))[:limit]
            else:
                wanted = self._search_ids(
                    client, str(query).strip(), datasources, limit
                )
            yield from self._read(client, wanted)

    def _search_ids(
        self, client: GleanClient, query: str, datasources: list[str], limit: int
    ) -> list[str]:
        ids: dict[str, None] = {}
        cursor: str | None = None
        pages = MAX_SEARCH_PAGES
        for _ in range(pages):
            response = client.search(
                query,
                page_size=min(SEARCH_PAGE_SIZE, limit),
                cursor=cursor,
                datasources=datasources,
            )
            results = response.get("results")
            for result in results if isinstance(results, list) else []:
                doc_id = search_hit(result)["document_id"]
                if doc_id:
                    ids.setdefault(doc_id, None)
                    if len(ids) >= limit:
                        return list(ids)
            cursor = (
                response.get("cursor")
                if isinstance(response.get("cursor"), str)
                else None
            )
            if response.get("hasMoreResults") is not True or not cursor:
                break
        else:
            # The cap, not Glean, ended the search: say so (counts only; no
            # query, ids or text).
            logger.warning(
                "glean-source: stopped the search at the %d-page cap with more "
                "results available; read %d of up to %d document(s)",
                pages,
                len(ids),
                limit,
            )
        return list(ids)

    def _read(self, client: GleanClient, ids: list[str]) -> Iterator[DataEnvelope]:
        skipped = 0
        hashed = 0
        for start in range(0, len(ids), READ_BATCH):
            batch = ids[start : start + READ_BATCH]
            readable, failed = split_documents(client.get_documents(ids=batch))
            skipped += len(failed)
            for doc in readable:
                record = document_record(doc)
                if not record["document_id"]:
                    skipped += 1
                    continue
                text = record.pop("text")
                metadata = {"glean_document_id": record.pop("document_id")}
                metadata.update(
                    {
                        k: v
                        for k, v in record.items()
                        if v is not None and k != "doc_type"
                    }
                )
                if "update_time" not in metadata:
                    # No updateTime from Glean: the text's hash is the marker.
                    metadata["content_sha256"] = hashlib.sha256(
                        text.encode("utf-8")
                    ).hexdigest()
                    hashed += 1
                yield DataEnvelope(
                    content_type="text/plain",
                    data=text,
                    source_id=self.name,
                    metadata=metadata,
                )
        if skipped:
            logger.info(
                "glean-source: skipped %d document(s) Glean could not read", skipped
            )
        if hashed:
            logger.info(
                "glean-source: %d document(s) had no updateTime; their version "
                "marker is content_sha256",
                hashed,
            )


__all__ = ["GleanAPIError", "GleanNotConfigured", "GleanRequestError", "GleanSource"]
