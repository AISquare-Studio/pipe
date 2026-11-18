"""Data API rows as plain JSON values, and the result's hashes.

``GetStatementResult`` sends each row as a list of ``Field`` unions in
column order; :func:`convert_page` turns them into objects keyed by column
name:

* ``isNull`` -> None; ``booleanValue`` -> bool; ``longValue`` -> int;
* ``doubleValue`` -> float (``NaN`` and the infinities as strings, so the
  JSON stays valid);
* ``stringValue`` -> the string Redshift sends (``NUMERIC``/``DECIMAL``
  stay exact strings, dates and timestamps as Redshift formats them);
* ``blobValue`` -> base64 text.

A column name that repeats (``SELECT a, a``) gets ``_2``, ``_3``... so no
value is lost.

:func:`result_sha256` hashes a result independently of row order (rows come
in no fixed order without ``ORDER BY``): the columns, then the sorted digests
of every row, so the same rows give the same hash.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Iterable, Iterator
from typing import Any


def columns(metadata: Any) -> list[dict[str, Any]]:
    """``ColumnMetadata`` as plain columns: unique ``name``, ``type``
    (Redshift's type name), ``nullable`` and, where set, ``label``,
    ``schema``, ``table``, ``precision``, ``scale`` and ``length``."""
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for item in metadata if isinstance(metadata, list) else []:
        if not isinstance(item, dict):
            item = {}
        base = str(item.get("name") or item.get("label") or f"column_{len(out) + 1}")
        name, count = base, 1
        while name in used:
            count += 1
            name = f"{base}_{count}"
        used.add(name)
        column: dict[str, Any] = {
            "name": name,
            "type": str(item.get("typeName") or ""),
            "nullable": item.get("nullable") != 0,
        }
        for key, source in (
            ("label", "label"),
            ("schema", "schemaName"),
            ("table", "tableName"),
            ("precision", "precision"),
            ("scale", "scale"),
            ("length", "length"),
        ):
            value = item.get(source)
            if value not in (None, ""):
                column[key] = value
        out.append(column)
    return out


def describe_columns(metadata: Any) -> list[dict[str, Any]]:
    """DescribeTable's ``ColumnList`` for a table's schema: ``name``,
    ``type``, ``nullable``, ``ordinal`` and, where set, ``length``,
    ``precision``, ``scale``, ``default``."""
    out: list[dict[str, Any]] = []
    for ordinal, item in enumerate(metadata if isinstance(metadata, list) else [], 1):
        if not isinstance(item, dict):
            continue
        column: dict[str, Any] = {
            "name": str(item.get("name") or ""),
            "type": str(item.get("typeName") or ""),
            "nullable": item.get("nullable") != 0,
            "ordinal": ordinal,
        }
        for key, source in (
            ("length", "length"),
            ("precision", "precision"),
            ("scale", "scale"),
            ("default", "columnDefault"),
        ):
            value = item.get(source)
            if value not in (None, ""):
                column[key] = value
        out.append(column)
    return out


def value(field: Any) -> Any:
    """One ``Field`` union as a JSON value."""
    if not isinstance(field, dict) or field.get("isNull"):
        return None
    if "booleanValue" in field:
        return bool(field["booleanValue"])
    if "longValue" in field:
        return int(field["longValue"])
    if "doubleValue" in field:
        number = float(field["doubleValue"])
        return number if math.isfinite(number) else str(number)
    if "stringValue" in field:
        return str(field["stringValue"])
    if "blobValue" in field:
        blob = field["blobValue"]
        raw = blob if isinstance(blob, (bytes, bytearray)) else str(blob).encode()
        return base64.b64encode(bytes(raw)).decode("ascii")
    return None


def convert_page(cols: list[dict[str, Any]], records: Any) -> list[dict[str, Any]]:
    """A page's ``Records`` as objects keyed by column name."""
    return list(iter_page(cols, records))


def iter_page(cols: list[dict[str, Any]], records: Any) -> Iterator[dict[str, Any]]:
    """:func:`convert_page`, one row at a time (a page's rows are never all
    objects at once)."""
    names = [c["name"] for c in cols]
    for record in records if isinstance(records, list) else []:
        cells = record if isinstance(record, list) else []
        yield {
            name: value(cells[index]) if index < len(cells) else None
            for index, name in enumerate(names)
        }


def canonical(obj: Any) -> bytes:
    """Stable JSON bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def row_digest(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(row)).hexdigest()


def rows_sha256(rows: list[dict[str, Any]]) -> str:
    """The rows' hash in their order (one batch)."""
    return digests_sha256(row_digest(row) for row in rows)


def digests_sha256(digests: Iterable[str]) -> str:
    """:func:`rows_sha256` from the rows' :func:`row_digest` values."""
    digest = hashlib.sha256()
    for row_hash in digests:
        digest.update(row_hash.encode("ascii"))
    return digest.hexdigest()


def result_sha256(cols: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """The whole result's hash, whatever the rows' order: the columns'
    names and types, then every row's digest, sorted."""
    return result_sha256_of_digests(cols, (row_digest(row) for row in rows))


def result_sha256_of_digests(cols: list[dict[str, Any]], digests: Iterable[str]) -> str:
    """:func:`result_sha256` from the rows' :func:`row_digest` values."""
    digest = hashlib.sha256()
    digest.update(canonical([[c["name"], c["type"]] for c in cols]))
    for row_hash in sorted(digests):
        digest.update(row_hash.encode("ascii"))
    return digest.hexdigest()


def envelope_json(obj: Any) -> bytes:
    """``obj`` as the source's envelopes carry it: ``json.dumps``'s own
    layout (keys in column order, ``", "`` and ``": "``), UTF-8."""
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


def envelope_batch(cols: list[dict[str, Any]], encoded: Iterable[bytes]) -> bytes:
    """``envelope_json({"columns": cols, "rows": rows})`` built from each
    row's :func:`envelope_json`, byte for byte, without turning the rows
    back into objects."""
    return (
        b'{"columns": '
        + envelope_json(cols)
        + b', "rows": ['
        + b", ".join(encoded)
        + b"]}"
    )


def json_size(row: dict[str, Any]) -> int:
    """The row's size as the JSON a caller gets."""
    return len(canonical(row))
