"""Tables' metadata from INFORMATION_SCHEMA: the version marker and the columns.

Both are fixed SELECTs on ``<database>.INFORMATION_SCHEMA``, with the schema
and table names sent as bind variables (never spliced into the SQL) and the
database as one quoted identifier. INFORMATION_SCHEMA shows only what the
role has a grant on, so "no row" means "does not exist, or not granted".

``LAST_ALTERED`` (``INFORMATION_SCHEMA.TABLES``) is the version marker: it
moves on every DML or DDL on the table, and also on Snowflake's background
maintenance (reclustering, ...). It never misses a change; it may move
without one, and the result's hash then shows nothing changed.
"""

from __future__ import annotations

from typing import Any

from aisquare_pipe_snowflake.client import SnowflakeClient
from aisquare_pipe_snowflake.refs import TableRef, quote

#: Tables whose marker one statement reads at most.
MAX_MARKER_TABLES = 20
#: A table type whose LAST_ALTERED moves with its data. A view's moves only
#: with its definition.
DATA_TABLE_TYPES = frozenset({"BASE TABLE"})
_ROWS = 1000
_BYTES = 4 * 1024 * 1024

_TABLES_SQL = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES, CREATED, LAST_ALTERED, "
    "COMMENT, CLUSTERING_KEY FROM {database}.INFORMATION_SCHEMA.TABLES WHERE {where}"
)
_COLUMNS_SQL = (
    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, CHARACTER_MAXIMUM_LENGTH, "
    "NUMERIC_PRECISION, NUMERIC_SCALE, COMMENT FROM {database}.INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION"
)


def _text(value: str) -> dict[str, str]:
    return {"type": "TEXT", "value": value}


def table_info(
    client: SnowflakeClient, refs: list[TableRef], *, timeout_seconds: float
) -> dict[TableRef, dict[str, Any]]:
    """INFORMATION_SCHEMA.TABLES' row for each of ``refs`` the role can see
    (at most :data:`MAX_MARKER_TABLES`), with one statement per database."""
    found: dict[TableRef, dict[str, Any]] = {}
    by_database: dict[str, list[TableRef]] = {}
    for ref in refs[:MAX_MARKER_TABLES]:
        by_database.setdefault(ref.database, []).append(ref)
    for database, group in by_database.items():
        where = " OR ".join("(TABLE_SCHEMA = ? AND TABLE_NAME = ?)" for _ in group)
        bindings: dict[str, dict[str, str]] = {}
        for ref in group:
            bindings[str(len(bindings) + 1)] = _text(ref.schema)
            bindings[str(len(bindings) + 1)] = _text(ref.name)
        result = client.execute(
            _TABLES_SQL.format(database=quote(database), where=where),
            row_limit=_ROWS,
            byte_limit=_BYTES,
            timeout_seconds=timeout_seconds,
            bindings=bindings,
        )
        for row in result.rows:
            ref = TableRef(
                database, str(row.get("TABLE_SCHEMA")), str(row.get("TABLE_NAME"))
            )
            if ref in group:
                found[ref] = row
    return found


def describe_table(
    client: SnowflakeClient, ref: TableRef, *, timeout_seconds: float
) -> dict[str, Any] | None:
    """The table's kind, size, ``last_altered`` and columns, or None when
    the role can't see it."""
    info = table_info(client, [ref], timeout_seconds=timeout_seconds).get(ref)
    if info is None:
        return None
    result = client.execute(
        _COLUMNS_SQL.format(database=quote(ref.database)),
        row_limit=_ROWS * 10,
        byte_limit=_BYTES,
        timeout_seconds=timeout_seconds,
        bindings={"1": _text(ref.schema), "2": _text(ref.name)},
    )
    columns = []
    for row in result.rows:
        column: dict[str, Any] = {
            "name": row.get("COLUMN_NAME"),
            "type": row.get("DATA_TYPE"),
            "nullable": str(row.get("IS_NULLABLE") or "").upper() == "YES",
        }
        for key, name in (
            ("COLUMN_DEFAULT", "default"),
            ("CHARACTER_MAXIMUM_LENGTH", "max_length"),
            ("NUMERIC_PRECISION", "precision"),
            ("NUMERIC_SCALE", "scale"),
            ("COMMENT", "comment"),
        ):
            if row.get(key) is not None:
                column[name] = row[key]
        columns.append(column)
    return {
        "table": ref.display(),
        "database": ref.database,
        "schema": ref.schema,
        "name": ref.name,
        "kind": info.get("TABLE_TYPE"),
        "row_count": info.get("ROW_COUNT"),
        "bytes": info.get("BYTES"),
        "created": info.get("CREATED"),
        "last_altered": info.get("LAST_ALTERED"),
        "comment": info.get("COMMENT"),
        "clustering_key": info.get("CLUSTERING_KEY"),
        "columns": columns,
        "columns_truncated": result.truncated is not None,
    }


__all__ = ["DATA_TABLE_TYPES", "MAX_MARKER_TABLES", "describe_table", "table_info"]
