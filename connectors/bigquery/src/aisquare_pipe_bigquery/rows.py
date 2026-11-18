"""BigQuery's REST rows as plain JSON values, and the result's hashes.

The REST API sends every row as ``{"f": [{"v": <cell>}, ...]}`` in schema
order, every scalar as a string: a ``RECORD`` cell is another ``{"f": [...]}``
and a ``REPEATED`` one a list of ``{"v": ...}``. :func:`convert_rows` turns
them into objects keyed by column name:

* ``INT64`` -> int; ``FLOAT64`` -> float (``NaN`` and the infinities stay
  strings, so the JSON stays valid); ``BOOL`` -> bool;
* ``TIMESTAMP`` -> RFC 3339 in UTC with microseconds (asked for as int64
  microseconds, ``formatOptions.useInt64Timestamp``, so nothing is rounded);
* ``NUMERIC``/``BIGNUMERIC`` stay strings (exact); ``DATE``, ``TIME``,
  ``DATETIME``, ``GEOGRAPHY`` (WKT), ``JSON``, ``INTERVAL``, ``RANGE`` and
  ``BYTES`` (base64) stay the strings BigQuery sends;
* ``RECORD``/``STRUCT`` -> an object, ``REPEATED`` -> a list, NULL -> None.

:func:`result_sha256` hashes a result independently of row order (BigQuery
returns rows in no fixed order without ``ORDER BY``): the schema, then the
sorted digests of every row, so the same rows give the same hash.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_INTEGER = frozenset({"INTEGER", "INT64"})
_FLOAT = frozenset({"FLOAT", "FLOAT64"})
_BOOL = frozenset({"BOOLEAN", "BOOL"})
_RECORD = frozenset({"RECORD", "STRUCT"})


def schema_fields(schema: Any) -> list[dict[str, Any]]:
    """A ``TableSchema`` (``{"fields": [...]}``) or a field list, as plain
    fields: ``name``, ``type``, ``mode`` (NULLABLE when absent),
    ``description`` when set, and ``fields`` for a record."""
    fields = schema.get("fields") if isinstance(schema, dict) else schema
    out: list[dict[str, Any]] = []
    for item in fields if isinstance(fields, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        field: dict[str, Any] = {
            "name": item["name"],
            "type": str(item.get("type") or "STRING").upper(),
            "mode": str(item.get("mode") or "NULLABLE").upper(),
        }
        if isinstance(item.get("description"), str) and item["description"]:
            field["description"] = item["description"]
        if field["type"] in _RECORD:
            field["fields"] = schema_fields(item.get("fields"))
        out.append(field)
    return out


def convert_rows(fields: list[dict[str, Any]], rows: Any) -> list[dict[str, Any]]:
    """REST rows (``[{"f": [...]}, ...]``) as objects keyed by column name."""
    return list(iter_rows(fields, rows))


def iter_rows(fields: list[dict[str, Any]], rows: Any) -> Iterator[dict[str, Any]]:
    """:func:`convert_rows`, one row at a time (a page's rows are never all
    objects at once)."""
    if not isinstance(rows, list):
        return
    for row in rows:
        yield _record(fields, row)


def _record(fields: list[dict[str, Any]], row: Any) -> dict[str, Any]:
    cells = row.get("f") if isinstance(row, dict) else None
    cells = cells if isinstance(cells, list) else []
    out: dict[str, Any] = {}
    for index, field in enumerate(fields):
        cell = cells[index] if index < len(cells) else None
        value = cell.get("v") if isinstance(cell, dict) else None
        out[field["name"]] = _value(field, value)
    return out


def _value(field: dict[str, Any], value: Any) -> Any:
    if value is None:
        return [] if field["mode"] == "REPEATED" else None
    if field["mode"] == "REPEATED":
        items = value if isinstance(value, list) else []
        scalar = {**field, "mode": "NULLABLE"}
        return [
            _value(scalar, item.get("v") if isinstance(item, dict) else item)
            for item in items
        ]
    kind = field["type"]
    if kind in _RECORD:
        return _record(field.get("fields") or [], value)
    if not isinstance(value, str):
        return value
    try:
        if kind in _INTEGER:
            return int(value)
        if kind in _FLOAT:
            number = float(value)
            return number if math.isfinite(number) else value
        if kind in _BOOL:
            return value.lower() == "true"
        if kind == "TIMESTAMP":
            return _timestamp(value)
    except (ValueError, OverflowError):
        return value
    return value


def _timestamp(value: str) -> str:
    """int64 microseconds (or float seconds, from an older format) since
    the epoch as RFC 3339 UTC with microseconds."""
    if value.lstrip("-").isdigit():
        moment = _EPOCH + timedelta(microseconds=int(value))
    else:
        moment = _EPOCH + timedelta(microseconds=round(float(value) * 1_000_000))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def millis_rfc3339(value: Any) -> str | None:
    """BigQuery's epoch-milliseconds string (``lastModifiedTime``,
    ``creationTime``) as RFC 3339 UTC, or None."""
    try:
        millis = int(str(value))
    except (TypeError, ValueError):
        return None
    try:
        moment = _EPOCH + timedelta(milliseconds=millis)
    except OverflowError:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def canonical(value: Any) -> bytes:
    """``value`` as canonical JSON (sorted keys, no spaces, UTF-8)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def row_size(row: dict[str, Any]) -> int:
    """The bytes ``row`` takes as JSON: what the byte caps count."""
    return len(canonical(row))


def rows_sha256(rows: list[dict[str, Any]]) -> str:
    """SHA-256 of ``rows`` in order (one batch's hash)."""
    return encoded_rows_sha256(canonical(row) for row in rows)


def encoded_rows_sha256(encoded: Iterable[bytes]) -> str:
    """:func:`rows_sha256` of rows already in :func:`canonical` form."""
    digest = hashlib.sha256()
    for row in encoded:
        digest.update(row)
        digest.update(b"\n")
    return digest.hexdigest()


def result_sha256(fields: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """SHA-256 of the schema and the rows, whatever order the rows came in."""
    return encoded_result_sha256(fields, (canonical(r) for r in rows))


def encoded_result_sha256(
    fields: list[dict[str, Any]], encoded: Iterable[bytes]
) -> str:
    """:func:`result_sha256` of rows already in :func:`canonical` form."""
    digest = hashlib.sha256(canonical(fields))
    for row_digest in sorted(hashlib.sha256(row).digest() for row in encoded):
        digest.update(row_digest)
    return digest.hexdigest()


def encoded_batch(fields: list[dict[str, Any]], encoded: Iterable[bytes]) -> bytes:
    """``canonical({"columns": fields, "rows": rows})`` built from the rows'
    :func:`canonical` forms, byte for byte (the keys sort as
    ``columns``, ``rows``), without turning them back into objects."""
    return (
        b'{"columns":' + canonical(fields) + b',"rows":[' + b",".join(encoded) + b"]}"
    )


__all__ = [
    "canonical",
    "convert_rows",
    "encoded_batch",
    "encoded_result_sha256",
    "encoded_rows_sha256",
    "iter_rows",
    "millis_rfc3339",
    "result_sha256",
    "row_size",
    "rows_sha256",
    "schema_fields",
]
