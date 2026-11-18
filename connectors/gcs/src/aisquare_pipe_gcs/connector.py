"""GcsSource: the files under a prefix of one Cloud Storage bucket, as envelopes.

``config`` is the tenant's credential blob (:mod:`.credentials`). Pull params
pick the files, never the credential, the bucket or the key:

* ``prefix`` (optional): a narrower prefix inside the configured one (default:
  the configured prefix), or ``object_names`` (a list of full names), not both;
* ``modified_after`` (optional, RFC 3339): only objects updated after it;
* ``max_objects`` (default 100, at most 1000) and ``max_bytes`` per object
  (default 25 MiB, at most 100 MiB; a larger object is skipped and counted).

Each object becomes one envelope with its bytes and ``metadata`` ``bucket``,
``object_name``, ``filename``, ``size``, ``content_type`` and the version
markers ``generation``, ``md5_hash``, ``updated`` (plus ``etag``,
``crc32c``, ``time_created``). A caller keeps them and re-reads the object
when ``generation`` (or ``md5_hash``) changes. "Folder" placeholders (names
ending in ``/``) are skipped.

A param naming a credential key (``service_account_json``, ``bucket``) is
refused, and so is a prefix or name outside the configured prefix. Skipped
objects are counted in one log line, with no names.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_gcs.client import (
    GcsAPIError,
    GcsClient,
    ObjectTooLarge,
    object_record,
)
from aisquare_pipe_gcs.credentials import (
    SERVER_KEYS,
    GcsCredentials,
    GcsNotConfigured,
    load_credentials,
)
from aisquare_pipe_gcs.toolkit import DEFAULT_MAX_BYTES, MAX_OBJECT_BYTES, file_envelope

logger = logging.getLogger("aisquare.pipe.gcs")

DEFAULT_MAX_OBJECTS = 100
MAX_OBJECTS = 1000
LIST_PAGE_SIZE = 1000
#: Listing pages followed at most, whatever the page token says.
MAX_LIST_PAGES = 100
_PARAMS = frozenset(
    {"prefix", "object_names", "modified_after", "max_objects", "max_bytes"}
)


def _options(params: PullParams | dict[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    raw = params.params if isinstance(params, PullParams) else params
    if not isinstance(raw, dict):
        raise TypeError("pull params must be a mapping")
    server = sorted(SERVER_KEYS.intersection(raw))
    if server:
        raise ValueError(
            f"pull param {server[0]!r} is server-controlled and cannot be set by a caller"
        )
    unknown = sorted(set(raw) - _PARAMS)
    if unknown:
        raise ValueError(f"unknown pull param {unknown[0]!r}")
    return raw


def _bounded(options: dict[str, Any], name: str, default: int, high: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise ValueError(f"{name} must be an integer from 1 to {high}")
    return value


def _timestamp(value: Any, what: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be an RFC 3339 time")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{what} must be an RFC 3339 time") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{what} must carry a timezone (e.g. Z)")
    return parsed


def _inside(creds: GcsCredentials, name: Any, what: str) -> str:
    if not isinstance(name, str) or not name or not name.isprintable():
        raise ValueError(f"{what} must be a non-empty object name")
    if not creds.contains(name):
        raise ValueError(f"{what} is outside the configured prefix {creds.prefix!r}")
    return name


class GcsSource(SourceConnector):
    """Pulls the objects under a bucket prefix, with their version markers."""

    name = "gcs-source"
    version = "0.1.0"
    # Framework class attributes, declared plain on SourceConnector.
    output_types = ["*/*"]  # noqa: RUF012
    auth_type = AuthType.CUSTOM
    description = (
        "Pulls the files under a Google Cloud Storage bucket prefix, with their version "
        "markers (generation, md5 hash, updated)."
    )
    docs_url = "https://cloud.google.com/storage/docs/json_api/v1/objects/list"

    CONFIG_SPEC: dict[str, MetaField] = {  # noqa: RUF012
        "service_account_json": MetaField(
            type=dict,
            required=True,
            description="The service account's JSON key (server-side only)",
        ),
        "bucket": MetaField(
            type=str, required=True, description="The bucket (server-side only)"
        ),
        "prefix": MetaField(
            type=str,
            required=False,
            description="Object-name prefix every read stays inside (server-side only)",
        ),
    }

    metadata_spec = {  # noqa: RUF012
        "filename": MetaField(
            type=str, required=True, description="The object name's last part"
        ),
        "bucket": MetaField(type=str, required=True, description="The bucket"),
        "object_name": MetaField(
            type=str, required=True, description="The object's full name"
        ),
        "size": MetaField(type=int, required=True, description="Size in bytes"),
        "content_type": MetaField(
            type=str, required=False, description="The object's content type"
        ),
        "generation": MetaField(
            type=str,
            required=False,
            description="Object generation: the version marker",
        ),
        "md5_hash": MetaField(
            type=str,
            required=False,
            description="Base64 MD5 of the content (absent for composite objects)",
        ),
        "updated": MetaField(
            type=str, required=False, description="RFC 3339 last-modified time"
        ),
        "etag": MetaField(type=str, required=False, description="The object's ETag"),
        "crc32c": MetaField(
            type=str, required=False, description="Base64 CRC32C of the content"
        ),
        "time_created": MetaField(
            type=str, required=False, description="RFC 3339 creation time"
        ),
    }

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def validate_config(self, config: dict) -> bool:
        try:
            load_credentials(config)
        except GcsNotConfigured:
            return False
        return True

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield one envelope per object. Raises :class:`GcsNotConfigured`
        without a usable credential (before any request), ``ValueError`` for
        bad params, and :class:`GcsAPIError` when Cloud Storage fails."""
        creds = load_credentials(config)
        options = _options(params)
        names = options.get("object_names")
        if names is not None and options.get("prefix") not in (None, ""):
            raise ValueError("give prefix or object_names, not both")
        if names is not None:
            if not isinstance(names, list) or not names:
                raise ValueError(
                    "object_names must be a non-empty list of object names"
                )
            names = [_inside(creds, n, "object_names") for n in dict.fromkeys(names)]
        prefix = options.get("prefix")
        prefix = (
            creds.prefix if prefix in (None, "") else _inside(creds, prefix, "prefix")
        )
        after = options.get("modified_after")
        since = None if after is None else _timestamp(after, "modified_after")
        limit = _bounded(options, "max_objects", DEFAULT_MAX_OBJECTS, MAX_OBJECTS)
        max_bytes = _bounded(options, "max_bytes", DEFAULT_MAX_BYTES, MAX_OBJECT_BYTES)

        with GcsClient(creds, session=self._session) as client:
            records = (
                [object_record(client.get_metadata(n)) for n in names[:limit]]
                if names is not None
                else self._listed(client, prefix, since, limit)
            )
            skipped = 0
            for record in records:
                if since is not None and not _after(record.get("updated"), since):
                    continue
                size = record.get("size")
                if isinstance(size, int) and size > max_bytes:
                    skipped += 1
                    continue
                try:
                    content = client.download(
                        str(record["name"]),
                        generation=record.get("generation"),
                        max_bytes=max_bytes,
                    )
                except ObjectTooLarge:
                    skipped += 1
                    continue
                yield file_envelope(record, content, bucket=creds.bucket)
            if skipped:
                logger.info(
                    "gcs-source: skipped %d object(s) larger than max_bytes", skipped
                )

    def _listed(
        self, client: GcsClient, prefix: str, since: datetime | None, limit: int
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_LIST_PAGES):
            page = client.list_objects(
                prefix=prefix, page_size=LIST_PAGE_SIZE, page_token=token
            )
            items = page.get("items")
            for item in items if isinstance(items, list) else []:
                record = object_record(item)
                name = record.get("name")
                if not isinstance(name, str) or not name or name.endswith("/"):
                    continue  # a "folder" placeholder
                if since is not None and not _after(record.get("updated"), since):
                    continue
                found.append(record)
                if len(found) >= limit:
                    return found
            next_token = page.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            token = next_token
        return found


def _after(updated: Any, since: datetime) -> bool:
    try:
        return _timestamp(updated, "updated") > since
    except ValueError:
        return True  # no usable time: keep it rather than lose a change


__all__ = ["GcsAPIError", "GcsNotConfigured", "GcsSource"]
