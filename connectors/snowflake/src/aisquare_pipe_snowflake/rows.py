"""Snowflake's SQL API rows as plain JSON values, and the result's hashes.

The SQL API (``jsonv2``) sends every cell as a string, or null for SQL NULL,
typed by the column's ``rowType`` entry. :func:`convert_rows` turns rows
into objects keyed by column name:

* ``fixed`` (NUMBER) with scale 0 -> int; with a scale it stays the exact
  decimal string; ``real`` (FLOAT) -> float (``NaN`` and the infinities stay
  strings, so the JSON stays valid); ``boolean`` -> bool;
* ``date`` (days since the epoch) -> ``YYYY-MM-DD``; ``time`` (seconds since
  midnight) -> ``HH:MM:SS[.fff...]``; ``timestamp_ntz`` -> ISO 8601 with no
  zone; ``timestamp_ltz`` -> ISO 8601 UTC (``Z``); ``timestamp_tz`` -> ISO
  8601 with its offset. Fractions keep the column's scale, so nothing is
  rounded;
* ``variant``, ``object``, ``array`` and ``vector`` -> the JSON value they
  hold; ``text``, ``binary`` (hex), ``geography``/``geometry`` and anything
  else stay the strings Snowflake sends.

:func:`result_sha256` hashes a result whatever its row order (Snowflake
returns rows in no fixed order without ``ORDER BY``): the columns, then the
sorted digests of every row.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_JSON_TYPES = frozenset({"variant", "object", "array", "vector", "map"})


def columns_of(row_type: Any) -> list[dict[str, Any]]:
    """``resultSetMetaData.rowType`` as the columns a result reports:
    ``name`` (unique: a repeated name gets ``_2``, ``_3``...; the original is
    ``source_name``), ``type`` (Snowflake's, upper-cased), ``nullable``, and
    ``precision``/``scale``/``length`` when Snowflake gives them."""
    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in row_type if isinstance(row_type, list) else []:
        if not isinstance(item, dict):
            continue
        original = str(item.get("name") or f"COLUMN{len(columns) + 1}")
        name, n = original, 1
        while name in seen:
            n += 1
            name = f"{original}_{n}"
        seen.add(name)
        column: dict[str, Any] = {
            "name": name,
            "type": str(item.get("type") or "text").upper(),
            "nullable": item.get("nullable") is not False,
        }
        if name != original:
            column["source_name"] = original
        for key in ("precision", "scale", "length"):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                column[key] = value
        columns.append(column)
    return columns


def convert_rows(columns: list[dict[str, Any]], rows: Any) -> list[dict[str, Any]]:
    """SQL API rows (lists of strings) as objects keyed by column name."""
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        cells = row if isinstance(row, list) else []
        out.append(
            {
                column["name"]: convert_value(
                    column, cells[index] if index < len(cells) else None
                )
                for index, column in enumerate(columns)
            }
        )
    return out


def convert_value(column: dict[str, Any], value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    kind = str(column.get("type") or "").lower()
    scale = column.get("scale")
    digits = scale if isinstance(scale, int) and 0 <= scale <= 9 else 9
    try:
        if kind == "fixed":
            return int(value) if not scale else value
        if kind == "real":
            number = float(value)
            return number if math.isfinite(number) else value
        if kind == "boolean":
            return value.strip().lower() in ("true", "1")
        if kind == "date":
            return (_EPOCH + timedelta(days=int(value))).date().isoformat()
        if kind == "time":
            seconds, nanos = _seconds(value)
            moment = _EPOCH + timedelta(seconds=seconds)
            return moment.strftime("%H:%M:%S") + _fraction(nanos, digits)
        if kind == "timestamp_ntz":
            seconds, nanos = _seconds(value)
            moment = _EPOCH + timedelta(seconds=seconds)
            return moment.strftime("%Y-%m-%dT%H:%M:%S") + _fraction(nanos, digits)
        if kind == "timestamp_ltz":
            seconds, nanos = _seconds(value)
            moment = _EPOCH + timedelta(seconds=seconds)
            return moment.strftime("%Y-%m-%dT%H:%M:%S") + _fraction(nanos, digits) + "Z"
        if kind == "timestamp_tz":
            text, _, offset_text = value.partition(" ")
            seconds, nanos = _seconds(text)
            offset = int(offset_text) - 1440 if offset_text else 0
            moment = _EPOCH + timedelta(seconds=seconds, minutes=offset)
            sign = "+" if offset >= 0 else "-"
            hours, minutes = divmod(abs(offset), 60)
            return (
                moment.strftime("%Y-%m-%dT%H:%M:%S")
                + _fraction(nanos, digits)
                + f"{sign}{hours:02d}:{minutes:02d}"
            )
        if kind in _JSON_TYPES:
            return json.loads(value)
    except (ValueError, OverflowError, InvalidOperation):
        return value
    return value


def _seconds(text: str) -> tuple[int, int]:
    """``"<seconds>[.<fraction>]"`` as whole seconds (floored) and
    nanoseconds, exactly."""
    total = Decimal(text.strip())
    seconds = math.floor(total)
    nanos = int((total - seconds) * 1_000_000_000)
    return seconds, nanos


def _fraction(nanos: int, digits: int) -> str:
    if digits <= 0:
        return ""
    return "." + f"{nanos:09d}"[:digits]


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
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical(row))
        digest.update(b"\n")
    return digest.hexdigest()


def result_sha256(columns: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """SHA-256 of the columns and the rows, whatever order the rows came in."""
    digest = hashlib.sha256(canonical(columns))
    for row_digest in sorted(hashlib.sha256(canonical(r)).digest() for r in rows):
        digest.update(row_digest)
    return digest.hexdigest()


__all__ = [
    "canonical",
    "columns_of",
    "convert_rows",
    "convert_value",
    "result_sha256",
    "row_size",
    "rows_sha256",
]
