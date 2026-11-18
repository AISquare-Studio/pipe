"""Read objects from the one bucket and prefix a credential blob names.

``ListObjectsV2`` (paged, 1000 keys a page) then ``GetObject``. Every key is
checked against the configured prefix first: a caller may narrow the prefix,
never leave it. Each object carries its version markers: ``ETag``,
``LastModified`` and, in a versioned bucket, ``VersionId``.

``version`` is the one marker to compare: the ``VersionId``, else the ``ETag``.
An object stored before versioning was enabled, or written while it is
suspended, has the ``VersionId`` ``"null"``. In a suspended bucket each write
replaces that null version, so the id stays ``"null"`` across edits: it counts
as no version id, and such an object's ``version`` is its ``ETag``.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterator, Mapping
from typing import Any

from aisquare_pipe_aws.agentcore import iso
from aisquare_pipe_aws.credentials import AwsAccessError, S3Location, call, pages


class S3ObjectTooLarge(AwsAccessError):
    """An object over the read limit (``invalid_arguments``): the source
    skips it, as it skips one the listing already shows too large."""


PAGE_SIZE = 1000
#: Largest object read into memory by default (25 MiB); larger ones are
#: listed but not downloaded.
DEFAULT_MAX_OBJECT_BYTES = 25 * 1024 * 1024
MAX_KEY = 1024


def client(session: Any, location: S3Location) -> Any:
    return session.client("s3", region_name=location.region)


def scoped_prefix(location: S3Location, prefix: str | None) -> str:
    """The prefix to list: the configured one, or a caller's narrower one
    inside it. Raises ``ValueError`` for one outside it."""
    if prefix is None or prefix == "":
        return location.prefix
    if not isinstance(prefix, str) or len(prefix) > MAX_KEY:
        raise ValueError(f"prefix must be a string of at most {MAX_KEY} characters")
    if not prefix.startswith(location.prefix):
        raise ValueError("prefix must stay inside the configured prefix")
    return prefix


def check_key(location: S3Location, key: Any) -> str:
    """``key`` when it is a key inside the configured prefix, else ``ValueError``."""
    if not isinstance(key, str) or not key or len(key) > MAX_KEY:
        raise ValueError(
            f"key must be a non-empty string of at most {MAX_KEY} characters"
        )
    if not key.startswith(location.prefix):
        raise ValueError("key must be inside the configured prefix")
    return key


def iter_objects(
    s3: Any, location: S3Location, prefix: str, *, start_after: str | None = None
) -> Iterator[dict[str, Any]]:
    """Every object under ``prefix``, every page (folder markers skipped)."""
    kwargs: dict[str, Any] = {
        "Bucket": location.bucket,
        "Prefix": prefix,
        "PaginationConfig": {"PageSize": PAGE_SIZE},
    }
    if start_after:
        kwargs["StartAfter"] = start_after
    listing = s3.get_paginator("list_objects_v2").paginate(**kwargs)
    for page in pages(listing, what="s3:ListObjectsV2"):
        for obj in page.get("Contents", []) or []:
            if str(obj.get("Key", "")).endswith("/") and not obj.get("Size"):
                continue
            yield obj


def list_page(
    s3: Any,
    location: S3Location,
    prefix: str,
    *,
    limit: int,
    continuation_token: str | None,
) -> dict[str, Any]:
    """One page of at most ``limit`` objects, for a tool call."""
    kwargs: dict[str, Any] = {
        "Bucket": location.bucket,
        "Prefix": prefix,
        "MaxKeys": limit,
    }
    if continuation_token:
        kwargs["ContinuationToken"] = continuation_token
    response = call(s3.list_objects_v2, what="s3:ListObjectsV2", **kwargs)
    items = [object_summary(location, o) for o in response.get("Contents", []) or []]
    return {
        "bucket": location.bucket,
        "prefix": prefix,
        "items": items,
        "has_more": bool(response.get("IsTruncated")),
        "next_continuation_token": response.get("NextContinuationToken"),
    }


def object_summary(location: S3Location, obj: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "key": obj.get("Key"),
        "size": obj.get("Size"),
        "etag": _etag(obj.get("ETag")),
        "last_modified": iso(obj.get("LastModified")),
        "storage_class": obj.get("StorageClass"),
        "bucket": location.bucket,
    }


def head_object(
    s3: Any, location: S3Location, key: str, *, version_id: str | None = None
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"Bucket": location.bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    response = call(s3.head_object, what="s3:HeadObject", **kwargs)
    return object_metadata(location, key, response)


def get_object(
    s3: Any,
    location: S3Location,
    key: str,
    *,
    version_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
) -> tuple[bytes, dict[str, Any]]:
    """``(body, metadata)``. An object over ``max_bytes`` is refused with an
    :class:`S3ObjectTooLarge` (an :class:`AwsAccessError`, code
    ``invalid_arguments``) before more than ``max_bytes + 1`` bytes are read."""
    kwargs: dict[str, Any] = {"Bucket": location.bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    response = call(s3.get_object, what="s3:GetObject", **kwargs)
    body = response["Body"]
    try:
        size = response.get("ContentLength")
        if isinstance(size, int) and size > max_bytes:
            raise S3ObjectTooLarge(
                f"object is {size} bytes, over the {max_bytes}-byte read limit",
                code="invalid_arguments",
            )
        data = _read(body, max_bytes + 1)
        if len(data) > max_bytes:
            raise S3ObjectTooLarge(
                f"object is over the {max_bytes}-byte read limit",
                code="invalid_arguments",
            )
    finally:
        body.close()
    return data, object_metadata(location, key, response)


def _read(body: Any, amount: int) -> bytes:
    """``body.read(amount)``. The body streams after GetObject has answered,
    outside :func:`~aisquare_pipe_aws.credentials.call`, so a read that fails
    there (a timeout, a dropped connection, a short body) is mapped here the
    same way: an :class:`AwsAccessError` naming the error's type only (its
    text names the bucket and key)."""
    from botocore.exceptions import BotoCoreError

    try:
        return bytes(body.read(amount))
    except BotoCoreError as e:
        raise AwsAccessError(
            f"AWS s3:GetObject failed while reading the object ({type(e).__name__})",
            code="upstream_error",
        ) from None


def object_metadata(
    location: S3Location, key: str, response: Mapping[str, Any]
) -> dict[str, Any]:
    content_type = response.get("ContentType") or ""
    if not content_type or content_type == "binary/octet-stream":
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    etag = _etag(response.get("ETag"))
    version_id = real_version_id(response.get("VersionId"))
    return {
        "bucket": location.bucket,
        "key": key,
        "etag": etag,
        "last_modified": iso(response.get("LastModified")),
        "version_id": version_id,
        "version": version_marker(version_id, etag),
        "size": response.get("ContentLength"),
        "content_type": content_type,
    }


#: The ``VersionId`` of an object stored before versioning was enabled or
#: written while it is suspended. It names no particular write, so it is not a
#: version marker.
NULL_VERSION_ID = "null"


def real_version_id(value: Any) -> str | None:
    """``value`` as a version id, or ``None`` for none (absent, empty, ``"null"``)."""
    if value is None:
        return None
    text = str(value)
    return None if text in ("", NULL_VERSION_ID) else text


def version_marker(version_id: str | None, etag: str | None) -> str:
    """The marker that changes when the object does: its version id, else its ETag."""
    return version_id or etag or ""


def _etag(value: Any) -> str | None:
    return None if value is None else str(value).strip('"')
