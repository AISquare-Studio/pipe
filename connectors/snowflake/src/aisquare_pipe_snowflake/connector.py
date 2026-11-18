"""SnowflakeQuerySource: a table's rows, or one SELECT's, as envelopes of row batches.

``config`` is the tenant's credential blob (:mod:`.credentials`). Pull params
pick the rows, never the account, the user, the key, the role or the
warehouse. Exactly one of:

* ``table``: ``DATABASE.SCHEMA.TABLE`` (or ``SCHEMA.TABLE`` / ``TABLE`` with
  the default database and schema), read with a SELECT the connector writes
  itself (every name a quoted identifier). Optional ``columns`` (column
  names; default all), ``order_by`` (column names: a stable order) and
  ``start_index`` (with ``order_by``: the first row; pass a truncated
  pull's ``next_start_index`` to continue it).
* ``sql``: ONE read-only Snowflake SELECT (or WITH ... SELECT), checked
  before any request (:mod:`.sqlguard`).

And for both: ``database`` and ``schema`` (the defaults for names written
without them; the blob's otherwise), ``batch_size`` rows per envelope
(default 500, at most 10,000) and ``max_batch_bytes`` bytes of envelope data
(default 256 KiB, what pipe-as-service returns inline in a synchronous pull;
at least 1 KiB, at most 64 MiB; a single larger row still gets an envelope
of its own), whichever comes first, ``max_rows`` (default 10,000, at most
100,000), ``max_result_bytes`` (JSON bytes of rows, default 25 MiB, at most
64 MiB, sized for a 512 MiB pod: see the README's "Memory") and
``timeout_seconds`` (the statement deadline, default 120, at
most 600).

Each envelope is one batch: ``data`` is JSON ``{"columns": [...], "rows":
[{column: value}]}`` (``application/json``; ``schema`` carries the columns
too), and ``metadata`` carries the version markers:

* ``result_sha256``: the whole result's hash (columns plus rows, whatever
  their order), the same on every batch of one pull. It changes when the
  data the pull read changes.
* ``table_last_altered``: ``INFORMATION_SCHEMA.TABLES.LAST_ALTERED`` of the
  table, read BEFORE the rows, so a change made while they are read shows at
  the next pull. For ``sql``, the latest over the tables the statement
  names, set only when every one is a base table the role can see (not for
  views, table functions, INFORMATION_SCHEMA...); ``tables_last_altered``
  lists the ones read.
* ``version``: ``<table_last_altered>|<result_sha256>``, or
  ``result_sha256`` alone without the time. Keep it and re-read when it
  changes; comparing ``table_last_altered`` with a fresh
  ``SNOWFLAKE_DESCRIBE_TABLE`` is the cheap check.

plus ``kind`` (``table`` or ``query``), ``table`` or ``query_sha256`` (the
statement's hash, never its text), ``query_id``, ``batch_index``,
``batch_count``, ``first_row``, ``row_count``, ``batch_sha256`` and
``result_row_count``. A result with no rows is one envelope with no rows,
so its markers still come out.

A result cut short by ``max_rows`` or ``max_result_bytes`` says so on every
envelope: ``truncated`` (``max_rows`` or ``max_result_bytes``), and for a
``table`` pull with ``order_by`` also ``next_start_index``. Its
``result_sha256`` covers the rows read only.

A param naming a credential key is refused, and so is SQL that is not one
SELECT. Logs carry no SQL, no table names and no row values.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.batches import INLINE_BATCH_BYTES, batch_bounds
from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_snowflake.client import (
    SnowflakeAPIError,
    SnowflakeClient,
    StatementResult,
)
from aisquare_pipe_snowflake.credentials import (
    SERVER_KEYS,
    SnowflakeCredentials,
    SnowflakeNotConfigured,
    load_credentials,
)
from aisquare_pipe_snowflake.metadata import (
    DATA_TABLE_TYPES,
    MAX_MARKER_TABLES,
    table_info,
)
from aisquare_pipe_snowflake.refs import (
    BadReference,
    TableRef,
    parse_identifier,
    parse_table,
    quote,
)
from aisquare_pipe_snowflake.rows import (
    encoded_batch,
    encoded_result_sha256,
    encoded_rows_sha256,
)
from aisquare_pipe_snowflake.sqlguard import (
    NotReadOnly,
    check_select_only,
    table_references,
)
from aisquare_pipe_snowflake.toolkit import SnowflakeRequestError, source_error

logger = logging.getLogger("aisquare.pipe.snowflake")

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 10_000
#: The smallest ``max_batch_bytes`` (the default is the host's inline size,
#: :data:`~aisquare.pipe.core.batches.INLINE_BATCH_BYTES`).
MIN_BATCH_BYTES = 1024
DEFAULT_MAX_ROWS = 10_000
MAX_ROWS = 100_000
DEFAULT_MAX_RESULT_BYTES = 25 * 1024 * 1024
#: Sized for a 512 MiB pod: the source peaks at about 1.35x the rows' JSON
#: plus one parsed partition (the README's "Memory" has the measurements),
#: and a host holds its envelopes too (pipe-as-service base64-encodes each,
#: ~1.33x).
MAX_RESULT_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
#: The metadata read (the marker) gets this much of the statement deadline.
MARKER_TIMEOUT = 30
MAX_COLUMNS = 200
_PARAMS = frozenset(
    {
        "table",
        "sql",
        "database",
        "schema",
        "columns",
        "order_by",
        "start_index",
        "batch_size",
        "max_batch_bytes",
        "max_rows",
        "max_result_bytes",
        "timeout_seconds",
    }
)
_TABLE_ONLY = ("columns", "order_by", "start_index")


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


def _bounded(
    options: dict[str, Any], name: str, default: int, low: int, high: int
) -> int:
    value = options.get(name, default)
    if value is None:
        value = default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _names(options: dict[str, Any], name: str) -> list[str] | None:
    value = options.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not value or len(value) > MAX_COLUMNS:
        raise ValueError(f"{name} must be a list of 1-{MAX_COLUMNS} column names")
    return [parse_identifier(item, name) for item in value]


class SnowflakeQuerySource(SourceConnector):
    """Pulls a table's rows, or one SELECT's, in batches with version markers."""

    name = "snowflake-query-source"
    version = "0.1.0"
    # Framework class attributes, declared plain on SourceConnector.
    output_types = ["application/json"]  # noqa: RUF012
    auth_type = AuthType.CUSTOM
    description = (
        "Pulls the rows of a Snowflake table, or of one read-only SELECT, as JSON batches "
        "with version markers (the result's hash and the table's LAST_ALTERED time)."
    )
    docs_url = "https://docs.snowflake.com/en/developer-guide/sql-api/reference"

    CONFIG_SPEC: dict[str, MetaField] = {  # noqa: RUF012
        "account": MetaField(
            type=str,
            required=True,
            description="The Snowflake account identifier, e.g. myorg-myaccount "
            "(server-side only)",
        ),
        "auth": MetaField(
            type=str,
            required=False,
            description="keypair (default) or oauth_client (server-side only)",
        ),
        "user": MetaField(
            type=str, required=False, description="The key-pair user (server-side only)"
        ),
        "private_key_pem": MetaField(
            type=str,
            required=False,
            description="The user's RSA private key, PEM (server-side only)",
        ),
        "role": MetaField(
            type=str, required=True, description="The read-only role (server-side only)"
        ),
        "warehouse": MetaField(
            type=str,
            required=True,
            description="The warehouse queries run on (server-side only)",
        ),
        "database": MetaField(
            type=str, required=False, description="The default database"
        ),
        "schema": MetaField(type=str, required=False, description="The default schema"),
    }

    metadata_spec = {  # noqa: RUF012
        "kind": MetaField(type=str, required=True, description="table or query"),
        "table": MetaField(
            type=str, required=False, description="DATABASE.SCHEMA.TABLE (table pulls)"
        ),
        "query_sha256": MetaField(
            type=str,
            required=False,
            description="SHA-256 of the statement run (query pulls; never its text)",
        ),
        "query_id": MetaField(
            type=str, required=False, description="Snowflake's query id"
        ),
        "batch_index": MetaField(type=int, required=True, description="0-based batch"),
        "batch_count": MetaField(type=int, required=True, description="Batches in all"),
        "first_row": MetaField(
            type=int, required=True, description="The batch's first row (0-based)"
        ),
        "row_count": MetaField(
            type=int, required=True, description="Rows in the batch"
        ),
        "batch_sha256": MetaField(
            type=str, required=True, description="SHA-256 of the batch's rows"
        ),
        "result_sha256": MetaField(
            type=str,
            required=True,
            description="SHA-256 of the whole result (columns and rows, any order): a "
            "version marker",
        ),
        "result_row_count": MetaField(
            type=int, required=True, description="Rows in the whole result"
        ),
        "table_last_altered": MetaField(
            type=str,
            required=False,
            description="INFORMATION_SCHEMA.TABLES.LAST_ALTERED of the table(s) read: a "
            "version marker",
        ),
        "tables_last_altered": MetaField(
            type=dict,
            required=False,
            description="Query pulls: each base table read's LAST_ALTERED",
        ),
        "version": MetaField(
            type=str,
            required=True,
            description="<table_last_altered>|<result_sha256>, or result_sha256",
        ),
        "truncated": MetaField(
            type=str,
            required=False,
            description="When a cap cut the result: max_rows or max_result_bytes",
        ),
        "next_start_index": MetaField(
            type=int,
            required=False,
            description="With truncated, table pulls with order_by: the start_index that "
            "continues",
        ),
    }

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def validate_config(self, config: dict) -> bool:
        try:
            load_credentials(config)
        except SnowflakeNotConfigured:
            return False
        return True

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield one envelope per batch of rows.

        Raises, as pipe-as-service answers them:

        * 422, a ``ValueError``: :class:`SnowflakeNotConfigured` without a
          usable credential (before any request); bad params; SQL that is
          not one SELECT (:class:`~aisquare_pipe_snowflake.sqlguard.NotReadOnly`);
          and what Snowflake refused that the caller can fix, a statement
          that does not compile or a missing table
          (:class:`~aisquare_pipe_snowflake.toolkit.SnowflakeRequestError`);
        * 502, a :class:`~aisquare.pipe.errors.PipelineError`: any other
          Snowflake failure (a refused credential or grant, throttling, a
          timeout, a 5xx), with ``retry_after`` when Snowflake gave one.

        Neither carries Snowflake's own error text."""
        creds = load_credentials(config)
        try:
            yield from self._pull(creds, params)
        except SnowflakeAPIError as e:
            raise source_error(e, creds) from None

    def _pull(
        self, creds: SnowflakeCredentials, params: PullParams | None
    ) -> Iterator[DataEnvelope]:
        options = _options(params)
        has_table = options.get("table") not in (None, "")
        has_sql = options.get("sql") not in (None, "")
        if has_table == has_sql:
            raise ValueError("give exactly one of table or sql")
        if has_sql:
            wrong = [k for k in _TABLE_ONLY if k in options]
            if wrong:
                raise ValueError(f"{wrong[0]} goes with table, not sql")
        try:
            database = (
                parse_identifier(options["database"], "database")
                if options.get("database") not in (None, "")
                else creds.database
            )
            schema = (
                parse_identifier(options["schema"], "schema")
                if options.get("schema") not in (None, "")
                else creds.schema
            )
            batch_size = (
                _bounded(options, "batch_size", DEFAULT_BATCH_SIZE, 1, MAX_BATCH_SIZE),
                _bounded(
                    options,
                    "max_batch_bytes",
                    INLINE_BATCH_BYTES,
                    MIN_BATCH_BYTES,
                    MAX_RESULT_BYTES,
                ),
            )
            max_rows = _bounded(options, "max_rows", DEFAULT_MAX_ROWS, 1, MAX_ROWS)
            max_bytes = _bounded(
                options,
                "max_result_bytes",
                DEFAULT_MAX_RESULT_BYTES,
                1,
                MAX_RESULT_BYTES,
            )
            timeout = _bounded(
                options, "timeout_seconds", DEFAULT_TIMEOUT, 1, MAX_TIMEOUT
            )
            if has_table:
                ref = parse_table(
                    options["table"], "table", database=database, schema=schema
                )
                columns = _names(options, "columns")
                order_by = _names(options, "order_by")
                start = _bounded(options, "start_index", 0, 0, 2**53)
                if start and order_by is None:
                    raise ValueError("start_index needs order_by (a stable row order)")
                statement = _table_select(ref, columns, order_by, start, max_rows)
            else:
                statement = check_select_only(options["sql"])
        except BadReference as e:
            raise ValueError(str(e)) from None
        with SnowflakeClient(creds, session=self._session) as client:
            if has_table:
                yield from self._table(
                    client,
                    ref,
                    statement,
                    start,
                    order_by is not None,
                    batch_size,
                    max_rows,
                    max_bytes,
                    timeout,
                )
            else:
                yield from self._query(
                    client,
                    statement,
                    database,
                    schema,
                    batch_size,
                    max_rows,
                    max_bytes,
                    timeout,
                )

    # -- the two kinds of pull ------------------------------------------------

    def _table(
        self,
        client: SnowflakeClient,
        ref: TableRef,
        statement: str,
        start: int,
        ordered: bool,
        batch_size: tuple[int, int],
        max_rows: int,
        max_bytes: int,
        timeout: int,
    ) -> Iterator[DataEnvelope]:
        # The marker, read before the rows.
        info = table_info(
            client, [ref], timeout_seconds=min(timeout, MARKER_TIMEOUT)
        ).get(ref)
        result = client.execute(
            statement,
            row_limit=max_rows,
            byte_limit=max_bytes,
            timeout_seconds=timeout,
            encoded=True,
        )
        common: dict[str, Any] = {"kind": "table", "table": ref.display()}
        if info is not None and info.get("TABLE_TYPE") in DATA_TABLE_TYPES:
            altered = info.get("LAST_ALTERED")
            if isinstance(altered, str):
                common["table_last_altered"] = altered
        if result.truncated is not None and ordered:
            common["next_start_index"] = start + result.count
        yield from _batches(
            result, common, batch_size, start, f"snowflake:{ref.display()}", "table"
        )

    def _query(
        self,
        client: SnowflakeClient,
        statement: str,
        database: str | None,
        schema: str | None,
        batch_size: tuple[int, int],
        max_rows: int,
        max_bytes: int,
        timeout: int,
    ) -> Iterator[DataEnvelope]:
        markers = _markers(
            client, statement, database, schema, min(timeout, MARKER_TIMEOUT)
        )
        result = client.execute(
            statement,
            row_limit=max_rows,
            byte_limit=max_bytes,
            timeout_seconds=timeout,
            database=database,
            schema=schema,
            encoded=True,
        )
        digest = hashlib.sha256(statement.encode()).hexdigest()
        common: dict[str, Any] = {"kind": "query", "query_sha256": digest, **markers}
        yield from _batches(
            result, common, batch_size, 0, f"snowflake:query:{digest[:16]}", "query"
        )


def _table_select(
    ref: TableRef,
    columns: list[str] | None,
    order_by: list[str] | None,
    start: int,
    max_rows: int,
) -> str:
    """The table pull's own SELECT: every name one quoted identifier, the
    numbers integers checked already."""
    picked = ", ".join(quote(c) for c in columns) if columns else "*"
    statement = f"SELECT {picked} FROM {ref.sql()}"
    if order_by:
        statement += " ORDER BY " + ", ".join(quote(c) for c in order_by)
        statement += f" LIMIT {max_rows + 1} OFFSET {start}"
    else:
        statement += f" LIMIT {max_rows + 1}"
    return statement


def _markers(
    client: SnowflakeClient,
    statement: str,
    database: str | None,
    schema: str | None,
    timeout: float,
) -> dict[str, Any]:
    """The query's tables' LAST_ALTERED, read before it runs: the latest as
    ``table_last_altered`` only when every table it names is a base table
    the role can see."""
    refs, complete = table_references(statement, database=database, schema=schema)
    if len(refs) > MAX_MARKER_TABLES:
        refs, complete = refs[:MAX_MARKER_TABLES], False
    times: dict[str, str] = {}
    if refs:
        try:
            found = table_info(client, refs, timeout_seconds=timeout)
        except SnowflakeAPIError as e:
            if e.timed_out or e.status in (None, 401, 429) or (e.status or 0) >= 500:
                raise
            found, complete = {}, False  # e.g. no USAGE on a database it names
        for ref in refs:
            info = found.get(ref)
            altered = info.get("LAST_ALTERED") if info is not None else None
            if (
                info is None
                or info.get("TABLE_TYPE") not in DATA_TABLE_TYPES
                or not isinstance(altered, str)
            ):
                complete = False
                continue
            times[ref.display()] = altered
    markers: dict[str, Any] = {"tables_last_altered": times}
    if complete and times:
        markers["table_last_altered"] = max(times.values())
    return markers


def _batches(
    result: StatementResult,
    common: dict[str, Any],
    batch_size: tuple[int, int],
    first: int,
    source_id: str,
    kind: str,
) -> Iterator[DataEnvelope]:
    """The result as envelopes of at most ``batch_size`` (rows, bytes of
    envelope data; one when empty), from the rows' canonical JSON
    (``encoded=True``). Each batch's rows are
    let go once its envelope is built, so the rows and the envelopes a
    consumer keeps add up to about one copy of the result."""
    rows: list[bytes | None] = list(result.encoded or [])
    result.encoded = None
    count = len(rows)
    whole = encoded_result_sha256(result.columns, (r for r in rows if r is not None))
    altered = common.get("table_last_altered")
    common = {
        **common,
        "result_sha256": whole,
        "result_row_count": count,
        "version": f"{altered}|{whole}" if altered else whole,
    }
    if result.query_id:
        common["query_id"] = result.query_id
    if result.truncated is not None:
        common["truncated"] = result.truncated
        logger.warning(
            "snowflake-query-source: a %s pull stopped at %s; the envelopes say where to "
            "continue (table pulls with order_by) or to narrow the query",
            kind,
            result.truncated,
        )
    bounds = batch_bounds(
        [len(r) if r is not None else 0 for r in rows],
        max_rows=batch_size[0],
        max_bytes=batch_size[1],
        header=len(encoded_batch(result.columns, [])) - 2,
        separator=1,
        closing=2,
    )
    for index, (offset, end) in enumerate(bounds):
        batch = [r for r in rows[offset:end] if r is not None]
        rows[offset:end] = [None] * (end - offset)
        envelope = DataEnvelope(
            content_type="application/json",
            data=encoded_batch(result.columns, batch),
            source_id=f"{source_id}#rows={first + offset}-{first + offset + len(batch)}",
            metadata={
                **common,
                "batch_index": index,
                "batch_count": len(bounds),
                "first_row": first + offset,
                "row_count": len(batch),
                "batch_sha256": encoded_rows_sha256(batch),
            },
            schema=json.loads(json.dumps({"fields": result.columns})),
        )
        del batch
        yield envelope


__all__ = [
    "NotReadOnly",
    "SnowflakeAPIError",
    "SnowflakeNotConfigured",
    "SnowflakeQuerySource",
    "SnowflakeRequestError",
]
