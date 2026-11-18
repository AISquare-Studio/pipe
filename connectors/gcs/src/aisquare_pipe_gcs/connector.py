"""GcsSource: the files under a prefix of one Cloud Storage bucket, as envelopes.

``config`` is the tenant's credential blob (:mod:`.credentials`). Pull params
pick the files, never the credential, the bucket or the key:

* ``prefix`` (optional): a narrower prefix inside the configured one (default:
  the configured prefix), or ``object_names`` (a list of full names, at most
  ``max_objects``), not both;
* ``start_offset`` (optional, with a prefix): list from this object name on
  (GCS ``startOffset``: names are listed in lexicographic order, this one
  included). Pass a truncated pull's ``next_start_offset`` to continue it;
* ``modified_after`` (optional, RFC 3339): only objects updated after it;
* ``max_objects`` (default 100, at most 1000) and ``max_bytes`` per object
  (default 25 MiB, at most 64 MiB, sized for a 512 MiB pod; a larger object
  is skipped and counted).

Each object becomes one envelope with its bytes and ``metadata`` ``bucket``,
``object_name``, ``filename``, ``size``, ``content_type`` and the version
markers ``generation``, ``md5_hash``, ``updated`` (plus ``etag``,
``crc32c``, ``time_created``, and ``content_encoding`` when the stored bytes
are gzip). A caller keeps them and re-reads the object when ``generation``
(or ``md5_hash``) changes. "Folder" placeholders (names ending in ``/``) are
skipped.

A listing that stops before its end says so: on ``max_objects``, or after
``MAX_LIST_PAGES`` pages (a ``modified_after`` that matches little in a big
prefix), the LAST envelope's metadata carries ``truncated`` (``max_objects``
or ``max_list_pages``) and ``next_start_offset``, and a warning is logged.
When no envelope came out (every name listed was older than
``modified_after``, or too large), there is no last envelope to carry them:
the pull raises :class:`GcsPullTruncated` instead, with ``reason`` and
``next_start_offset``. A caller that keeps ``max(updated)`` as a watermark
must finish a truncated pull first, or it misses changes.

A param naming a credential key (``service_account_json``, ``bucket``) is
refused, and so is a prefix or name outside the configured prefix. Skipped
objects are counted in one log line; logs carry no object names.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_gcs.client import (
    INVALID_NAMES,
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
from aisquare_pipe_gcs.toolkit import (
    DEFAULT_MAX_BYTES,
    GcsRequestError,
    file_envelope,
    source_error,
)

logger = logging.getLogger("aisquare.pipe.gcs")

DEFAULT_MAX_OBJECTS = 100
#: The largest object a pull reads (``max_bytes``'s ceiling), sized for a
#: 512 MiB pod: a pull holds about two objects at a time (the one being read,
#: and the one a consumer still holds); the README's "Memory" has the numbers.
#: Tools read up to MAX_OBJECT_BYTES.
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_OBJECTS = 1000
LIST_PAGE_SIZE = 1000
#: Listing pages followed at most, whatever the page token says.
MAX_LIST_PAGES = 100
_PARAMS = frozenset(
    {
        "prefix",
        "object_names",
        "start_offset",
        "modified_after",
        "max_objects",
        "max_bytes",
    }
)
TRUNCATED_MAX_OBJECTS = "max_objects"
TRUNCATED_MAX_LIST_PAGES = "max_list_pages"


class GcsPullTruncated(ValueError):
    """A pull stopped early (``reason``: ``max_objects`` or
    ``max_list_pages``) before any envelope came out, so no envelope can
    carry where to go on. Pull again with ``start_offset`` =
    ``next_start_offset`` (None when the resume point couldn't be read: retry
    the pull, or narrow the prefix or the ``modified_after`` window).

    A ``ValueError``, so a host that answers those as a client error (as
    pipe-as-service does, 422) passes the message, which carries the name to
    continue at, to the caller. Logs never carry it."""

    def __init__(self, reason: str, next_start_offset: str | None) -> None:
        self.reason = reason
        self.next_start_offset = next_start_offset
        how = (
            f"continue with start_offset={next_start_offset!r}"
            if next_start_offset is not None
            else "the name to continue at could not be read: retry the pull"
        )
        super().__init__(
            f"gcs-source: the listing stopped early ({reason}) before any object came "
            f"out; {how}, or narrow the prefix or the modified_after window"
        )


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
    if (
        not isinstance(name, str)
        or not name
        or not name.isprintable()
        or len(name.encode()) > 1024
        or name in INVALID_NAMES
    ):
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
        "content_encoding": MetaField(
            type=str,
            required=False,
            description="The stored encoding (gzip: size and md5_hash describe the "
            "compressed bytes; data is inflated)",
        ),
        "truncated": MetaField(
            type=str,
            required=False,
            description="On the last envelope of a pull that stopped early: "
            "max_objects or max_list_pages",
        ),
        "next_start_offset": MetaField(
            type=str,
            required=False,
            description="With truncated: the start_offset that continues the pull",
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
        """Yield one envelope per object.

        Raises, as pipe-as-service answers them:

        * 422, a ``ValueError``: :class:`GcsNotConfigured` without a usable
          credential (before any request); bad params;
          :class:`GcsPullTruncated`; and what Cloud Storage refused that the
          caller can fix, a named object that does not exist
          (:class:`~aisquare_pipe_gcs.toolkit.GcsRequestError`);
        * 502, a :class:`~aisquare.pipe.errors.PipelineError`: any other
          Cloud Storage failure (a refused key or permission, throttling, a
          timeout, a 5xx), with ``retry_after`` when it gave one.

        Neither carries Cloud Storage's own error text."""
        creds = load_credentials(config)
        try:
            yield from self._pull(creds, params)
        except GcsAPIError as e:
            raise source_error(e) from None

    def _pull(
        self, creds: GcsCredentials, params: PullParams | None
    ) -> Iterator[DataEnvelope]:
        options = _options(params)
        limit = _bounded(options, "max_objects", DEFAULT_MAX_OBJECTS, MAX_OBJECTS)
        max_bytes = _bounded(options, "max_bytes", DEFAULT_MAX_BYTES, MAX_SOURCE_BYTES)
        names = options.get("object_names")
        if names is not None and (
            options.get("prefix") not in (None, "")
            or options.get("start_offset") not in (None, "")
        ):
            raise ValueError("give prefix/start_offset or object_names, not both")
        if names is not None:
            if not isinstance(names, list) or not names:
                raise ValueError(
                    "object_names must be a non-empty list of object names"
                )
            names = [_inside(creds, n, "object_names") for n in dict.fromkeys(names)]
            if len(names) > limit:
                raise ValueError(
                    f"object_names has {len(names)} names, over max_objects ({limit})"
                )
        prefix = options.get("prefix")
        prefix = (
            creds.prefix if prefix in (None, "") else _inside(creds, prefix, "prefix")
        )
        start = options.get("start_offset")
        if start in (None, ""):
            start = None
        else:
            start = _inside(creds, start, "start_offset")
            if not start.startswith(prefix):
                raise ValueError(f"start_offset is outside the prefix {prefix!r}")
        after = options.get("modified_after")
        since = None if after is None else _timestamp(after, "modified_after")

        with GcsClient(creds, session=self._session) as client:
            truncation: tuple[str, str | None] | None = None
            if names is not None:
                records = [object_record(client.get_metadata(n)) for n in names]
            else:
                records, truncation = self._listed(client, prefix, start, since, limit)
            skipped = 0
            candidates: list[dict[str, Any]] = []
            for record in records:
                generation = record.get("generation")
                if generation is not None and not isinstance(generation, str):
                    # Cloud Storage sends int64s as strings: anything else is
                    # its answer gone wrong, not the caller's to fix (502).
                    raise GcsAPIError(
                        "Cloud Storage answered an object whose generation is "
                        "not a string"
                    )
                if since is not None and not _after(record.get("updated"), since):
                    continue
                size = record.get("size")
                if isinstance(size, int) and size > max_bytes:
                    skipped += 1
                    continue
                candidates.append(record)
            del records
            # A truncated pull's LAST envelope carries the marker. Each one is
            # yielded at once, unless every candidate after it could still be
            # skipped at download (only an object of unknown size, or one
            # stored gzip and read inflated, can be): only then is it held
            # back, so at most one object is in memory as a rule.
            sure_later = [False] * len(candidates)
            seen_sure = False
            for index in range(len(candidates) - 1, -1, -1):
                sure_later[index] = seen_sure
                seen_sure = seen_sure or not _may_be_skipped(candidates[index])
            held: DataEnvelope | None = None
            yielded = 0
            for index, record in enumerate(candidates):
                try:
                    content = client.download(
                        str(record["name"]),
                        generation=record.get("generation"),
                        max_bytes=max_bytes,
                    )
                except ObjectTooLarge:
                    skipped += 1
                    continue
                envelope = file_envelope(record, content, bucket=creds.bucket)
                del content
                if held is not None:
                    yield held  # a later one came out: it wasn't the last
                    held = None
                    yielded += 1
                if truncation is None or sure_later[index]:
                    yield envelope
                    yielded += 1
                elif index == len(candidates) - 1:
                    _mark(envelope, truncation)
                    yield envelope
                    yielded += 1
                else:
                    held = envelope
                del envelope  # gone before the next download
            if skipped:
                logger.info(
                    "gcs-source: skipped %d object(s) larger than max_bytes", skipped
                )
            if truncation is not None:
                reason, resume = truncation
                logger.warning(
                    "gcs-source: the listing stopped early (%s): more objects remain "
                    "under the prefix; continue with %s as start_offset",
                    reason,
                    "the last envelope's next_start_offset"
                    if held is not None or yielded
                    else "the error's next_start_offset (no envelope came out)",
                )
                if held is not None:
                    _mark(held, truncation)
                    yield held
                elif not yielded:
                    raise GcsPullTruncated(reason, resume)

    def _listed(
        self,
        client: GcsClient,
        prefix: str,
        start: str | None,
        since: datetime | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], tuple[str, str | None] | None]:
        """Up to ``limit`` objects in name order, and why the listing stopped
        early (with the name to resume at), or None when it reached its end."""
        found: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_LIST_PAGES):
            page = client.list_objects(
                prefix=prefix,
                page_size=LIST_PAGE_SIZE,
                page_token=token,
                start_offset=start,
            )
            items = page.get("items")
            for item in items if isinstance(items, list) else []:
                record = object_record(item)
                name = record.get("name")
                if not isinstance(name, str) or not name:
                    continue
                if len(found) >= limit:
                    # A name after the last one kept: the pull is cut short.
                    return found, (TRUNCATED_MAX_OBJECTS, name)
                if name.endswith("/"):
                    continue  # a "folder" placeholder
                if since is not None and not _after(record.get("updated"), since):
                    continue
                found.append(record)
            next_token = page.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return found, None
            token = next_token
        reason = (
            TRUNCATED_MAX_OBJECTS if len(found) >= limit else TRUNCATED_MAX_LIST_PAGES
        )
        return found, (reason, self._first_name(client, prefix, start, token))

    @staticmethod
    def _first_name(
        client: GcsClient, prefix: str, start: str | None, token: str | None
    ) -> str | None:
        """The first name on the page ``token`` points at: where a pull cut
        short by the page cap resumes. None if it can't be read."""
        try:
            page = client.list_objects(
                prefix=prefix, page_size=1, page_token=token, start_offset=start
            )
        except GcsAPIError:
            return None
        items = page.get("items")
        for item in items if isinstance(items, list) else []:
            name = object_record(item).get("name")
            if isinstance(name, str) and name:
                return name
        return None


def _may_be_skipped(record: dict[str, Any]) -> bool:
    """Whether a download of ``record`` could still come out over
    ``max_bytes`` (its stored size was checked already): only when that
    size is unknown, or the bytes are stored gzip and read inflated."""
    return not isinstance(record.get("size"), int) or (
        str(record.get("content_encoding") or "").lower() == "gzip"
    )


def _mark(envelope: DataEnvelope, truncation: tuple[str, str | None]) -> None:
    reason, resume = truncation
    envelope.metadata["truncated"] = reason
    if resume is not None:
        envelope.metadata["next_start_offset"] = resume


def _after(updated: Any, since: datetime) -> bool:
    try:
        return _timestamp(updated, "updated") > since
    except ValueError:
        return True  # no usable time: keep it rather than lose a change


__all__ = [
    "GcsAPIError",
    "GcsNotConfigured",
    "GcsPullTruncated",
    "GcsRequestError",
    "GcsSource",
]
