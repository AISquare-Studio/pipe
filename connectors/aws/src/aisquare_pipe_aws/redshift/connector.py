"""RedshiftQuerySource: one SELECT's rows, or a table's, as envelopes of row batches.

``config`` is the tenant's credential blob (:mod:`.config`). Pull params pick
the rows, never the role, region, cluster, workgroup, database, user or
secret (a param naming one is refused). Exactly one of:

* ``sql``: ONE read-only Redshift statement (SELECT or WITH ... SELECT),
  checked before any AWS call (:mod:`.sqlguard`);
* ``table``: ``schema.table`` (plain names: letters, digits, ``_``, ``$``),
  read as ``SELECT * FROM "schema"."table"``, which passes the same check.

And for both: ``batch_size`` rows per envelope (default 500, at most 10,000)
and ``max_batch_bytes`` bytes of envelope data (default 256 KiB, what
pipe-as-service returns inline in a synchronous pull; at least 1 KiB, at
most 64 MiB; a single larger row still gets an envelope of its own),
whichever comes first, ``max_rows`` (default 10,000, at most 100,000),
``max_result_bytes`` (bytes of the envelopes' data in all, each one's
columns header included; default 25 MiB, at most 64 MiB, sized for a 512 MiB
pod: see the README's "Memory") and ``timeout_seconds`` (default 120, at
most 600; never more than the host's time budget): the statement is
cancelled if it runs past it.

Each envelope is one batch: ``data`` is JSON ``{"columns": [...], "rows":
[{column: value}]}`` (``application/json``), and ``metadata`` carries the
version markers:

* ``result_sha256``: the whole result's hash (columns plus rows, whatever
  their order), the same on every batch of one pull. It changes when the
  rows the pull read change;
* ``version``: ``result_sha256``. Keep it and re-read when it changes.

plus ``batch_index``, ``batch_count``, ``first_row``, ``row_count``,
``batch_sha256``, ``result_row_count``, ``total_rows``, ``statement_id``,
``database``, the cluster or workgroup, and ``table`` or ``query_sha256``
(the statement's hash, never its text). A result with no rows is one
envelope with no rows, so its markers still come out. A result cut short by
``max_rows`` or ``max_result_bytes`` says so on every envelope
(``truncated``); its ``result_sha256`` covers the rows read only.

A pull fails as pipe-as-service answers it (it maps a ``PipeError`` by its
exact type, a ``ValueError`` along the MRO):

* 422, a ``ValueError``: a blob that doesn't parse
  (``RedshiftNotConfiguredError``), bad params (``ConfigValidationError``),
  SQL that is not one SELECT (``NotReadOnly``), what the caller can fix that
  Redshift refused (:class:`RedshiftRequestError`: a statement that failed
  on its SQL, a relation that does not exist) and AWS's ``not_found`` and
  ``invalid_arguments`` (``AwsRequestError``);
* 502, a :class:`~aisquare.pipe.errors.PipelineError`: the deadline
  (cancelled or not), a permission the database user lacks, an aborted
  statement, and everything else AWS refused or failed (AssumeRole,
  throttling, an expired session, a 5xx).

Neither carries the database's error text (it can echo the caller's values)
or AWS's message: the toolkit, which answers the caller, still shows the
database's.

The connector's own logs carry no SQL, no table names and no row values, and
nothing sqlglot logs while the guard parses gets out
(:mod:`aisquare_pipe_aws.redshift.sqlguard`). botocore's DEBUG request log,
off unless a host turns it on, does hold the SQL.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from typing import Any

from aisquare.pipe import time_budget
from aisquare.pipe.core.batches import INLINE_BATCH_BYTES, batch_bounds
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams
from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_aws.connector import _AwsSource
from aisquare_pipe_aws.redshift import dataapi, rows
from aisquare_pipe_aws.redshift.config import (
    RedshiftConfig,
    RedshiftTarget,
    is_configured,
    refuse_server_keys,
)
from aisquare_pipe_aws.redshift.query import Result, run_select
from aisquare_pipe_aws.redshift.sqlguard import quote_identifier

VERSION = "0.1.0"

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 10_000
#: The smallest ``max_batch_bytes`` (the default is the host's inline size,
#: :data:`~aisquare.pipe.core.batches.INLINE_BATCH_BYTES`).
MIN_BATCH_BYTES = 1024
DEFAULT_MAX_ROWS = 10_000
MAX_ROWS = 100_000
DEFAULT_MAX_RESULT_BYTES = 25 * 1024 * 1024
#: Sized for a 512 MiB pod: the source peaks at about 1.35x the rows' JSON
#: plus a page or two (the README's "Memory" has the measurements), and a
#: host holds its envelopes too (pipe-as-service base64-encodes each, ~1.33x).
MAX_RESULT_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

PARAMS = frozenset(
    {
        "sql",
        "table",
        "batch_size",
        "max_batch_bytes",
        "max_rows",
        "max_result_bytes",
        "timeout_seconds",
    }
)
#: ``schema.table``: two plain Redshift names.
_TABLE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_$]{0,126})\.([A-Za-z_][A-Za-z0-9_$]{0,126})$"
)


def _options(params: PullParams | dict[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    raw = params.params if isinstance(params, PullParams) else params
    if not isinstance(raw, dict):
        raise ConfigValidationError("pull params must be a mapping")
    refuse_server_keys(raw, what="pull param")
    unknown = sorted(set(raw) - PARAMS)
    if unknown:
        raise ConfigValidationError(
            f"unsupported pull param(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(PARAMS))})"
        )
    return dict(raw)


def _int(values: dict[str, Any], name: str, default: int, ceiling: int) -> int:
    value = values.get(name)
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= ceiling
    ):
        raise ConfigValidationError(f"{name} must be an integer from 1 to {ceiling}")
    return value


def _statement(values: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The SQL to run and the metadata naming what it reads."""
    sql, table = values.get("sql"), values.get("table")
    if (sql is None) == (table is None):
        raise ConfigValidationError("pass exactly one of sql and table")
    if table is not None:
        match = _TABLE.match(table) if isinstance(table, str) else None
        if match is None:
            raise ConfigValidationError(
                "table must be schema.table (letters, digits, _ and $)"
            )
        schema, name = match.groups()
        text = f"SELECT * FROM {quote_identifier(schema)}.{quote_identifier(name)}"
        return text, {"table": f"{schema}.{name}"}
    if not isinstance(sql, str) or not sql.strip():
        raise ConfigValidationError("sql must be a non-empty string")
    return sql, {"query_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest()}


class RedshiftRequestError(ValueError):
    """Redshift ran the statement and it failed on what the caller sent (its
    SQL, a relation that does not exist or the user can't see). A
    ``ValueError``, so pipe-as-service answers it 422. ``code`` is the
    toolkit error code; the database's own text is not in the message."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


#: StatementFailed codes the caller can fix by asking differently.
_CALLER_CODES = frozenset({"invalid_arguments", "not_found"})
_HINTS = {
    "invalid_arguments": "Redshift could not run it as sent; REDSHIFT_RUN_SELECT shows "
    "the database's own error",
    "not_found": "a relation or column it names does not exist, or the database user "
    "has no grant on it",
    "forbidden": "the database user lacks a grant for this (the README's Redshift "
    "registration checklist, step 3)",
    "upstream_error": "Redshift aborted it (a cancel, a WLM rule or a restart); retry",
}


def statement_error(e: dataapi.StatementTimeout | dataapi.StatementFailed) -> Exception:
    """A statement that timed out or failed, as the source raises it:
    :class:`RedshiftRequestError` (422) for what the caller can fix, a
    :class:`~aisquare.pipe.errors.PipelineError` (502) otherwise. The
    message is the connector's own (never the database's text)."""
    if isinstance(e, dataapi.StatementTimeout):
        return PipelineError(str(e))
    hint = _HINTS.get(e.code)
    text = f"{e.summary} ({e.code})" + (f" — {hint}" if hint else "")
    if e.code in _CALLER_CODES:
        return RedshiftRequestError(text, code=e.code)
    return PipelineError(text)


class RedshiftQuerySource(_AwsSource):
    """One read-only SELECT's rows (or a table's) from Amazon Redshift, as JSON batches.

    An :class:`~aisquare_pipe_aws.connector._AwsSource`: its ``auth_type``, its
    ``base_session`` (the service's own principal) and its ``_session``, which
    assumes the customer role through
    :func:`aisquare_pipe_aws.credentials.assume_role_session`, are the AWS
    sources' own."""

    name = "redshift-query-source"
    version = VERSION
    description = (
        "Pulls the rows of one read-only SELECT, or of a table, from Amazon Redshift (provisioned "
        "or Serverless) through the Data API on a cross-account AWS role, as JSON batches with a "
        "version marker (the result's hash)."
    )
    docs_url = "https://docs.aws.amazon.com/redshift/latest/mgmt/data-api.html"
    output_types = ["application/json"]  # noqa: RUF012 — framework contract: class-level spec, never mutated
    metadata_spec = {  # noqa: RUF012 — framework contract: class-level spec, never mutated
        "version": MetaField(
            str, required=True, description="Version marker: result_sha256"
        ),
        "result_sha256": MetaField(
            str, required=True, description="The whole result's hash, order-independent"
        ),
        "batch_index": MetaField(
            int, required=True, description="0-based batch number"
        ),
        "batch_count": MetaField(
            int, required=True, description="Batches in this pull"
        ),
        "first_row": MetaField(
            int, required=True, description="0-based index of the first row"
        ),
        "row_count": MetaField(int, required=True, description="Rows in this batch"),
        "batch_sha256": MetaField(
            str, required=True, description="This batch's rows' hash"
        ),
        "result_row_count": MetaField(
            int, required=True, description="Rows this pull read"
        ),
        "total_rows": MetaField(int, description="Rows the statement returned in all"),
        "statement_id": MetaField(
            str, required=True, description="Data API statement id"
        ),
        "database": MetaField(str, required=True, description="The Redshift database"),
        "cluster_identifier": MetaField(str, description="The provisioned cluster"),
        "workgroup_name": MetaField(str, description="The Serverless workgroup"),
        "table": MetaField(str, description="schema.table, for a table pull"),
        "query_sha256": MetaField(
            str, description="The statement's hash, for a sql pull"
        ),
        "truncated": MetaField(
            str, description="max_rows | max_result_bytes, when cut short"
        ),
    }

    def validate_config(self, config: dict) -> bool:
        return is_configured(config)

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield one envelope per batch of rows; fails as the module
        docstring says (the AWS refusals through ``_AwsSource.pull``)."""
        try:
            yield from super().pull(config, params)
        except (dataapi.StatementTimeout, dataapi.StatementFailed) as e:
            raise statement_error(e) from None

    def _pull(self, config: dict, params: PullParams | None) -> Iterator[DataEnvelope]:
        cfg = RedshiftConfig.from_config(config)
        values = _options(params)
        sql, what = _statement(values)
        batch_size = _int(values, "batch_size", DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE)
        batch_bytes = _int(
            values, "max_batch_bytes", INLINE_BATCH_BYTES, MAX_RESULT_BYTES
        )
        if batch_bytes < MIN_BATCH_BYTES:
            raise ConfigValidationError(
                f"max_batch_bytes must be an integer from {MIN_BATCH_BYTES} to "
                f"{MAX_RESULT_BYTES}"
            )
        max_rows = _int(values, "max_rows", DEFAULT_MAX_ROWS, MAX_ROWS)
        max_bytes = _int(
            values, "max_result_bytes", DEFAULT_MAX_RESULT_BYTES, MAX_RESULT_BYTES
        )
        timeout = float(
            _int(
                values, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS
            )
        )
        budget = time_budget(config)
        if budget is not None:
            timeout = min(timeout, budget)
        result = run_select(
            sql,
            open_session=lambda: self._session(cfg.aws),
            target=cfg.target,
            deadline=dataapi.Deadline(timeout),
            max_rows=max_rows,
            max_result_bytes=max_bytes,
            encoded=True,
            batching=(batch_size, batch_bytes),
        )
        yield from _envelopes(result, cfg.target, what, (batch_size, batch_bytes))


def _envelopes(
    result: Result,
    target: RedshiftTarget,
    what: dict[str, Any],
    batch_size: tuple[int, int],
) -> Iterator[DataEnvelope]:
    """The result (read ``encoded=True``) as envelopes of at most
    ``batch_size`` (rows, bytes of envelope data; one when empty). Each
    batch's rows are let go once its envelope is
    built, so the rows and the envelopes a consumer keeps add up to about
    one copy of the result."""
    result_hash = result.result_sha256
    encoded: list[bytes | None] = list(result.encoded or [])
    digests = result.digests or []
    result.encoded = result.digests = None
    count = len(encoded)
    bounds = (
        result.plan.bounds()
        if result.plan is not None
        else batch_bounds(
            [len(r) if r is not None else 0 for r in encoded],
            max_rows=batch_size[0],
            max_bytes=batch_size[1],
            header=len(rows.envelope_batch(result.columns, [])) - 2,
            separator=2,
            closing=2,
        )
    )
    where = target.cluster_identifier or target.workgroup_name
    label = what.get("table") or f"query:{what['query_sha256'][:16]}"
    for index, (start, end) in enumerate(bounds):
        batch = [r for r in encoded[start:end] if r is not None]
        encoded[start:end] = [None] * (end - start)
        metadata: dict[str, Any] = {
            "version": result_hash,
            "result_sha256": result_hash,
            "batch_index": index,
            "batch_count": len(bounds),
            "first_row": start,
            "row_count": len(batch),
            "batch_sha256": rows.digests_sha256(digests[start:end]),
            "result_row_count": count,
            "statement_id": result.statement_id,
            "database": target.database,
            **what,
        }
        if result.total_rows is not None:
            metadata["total_rows"] = result.total_rows
        if target.cluster_identifier:
            metadata["cluster_identifier"] = target.cluster_identifier
        if target.workgroup_name:
            metadata["workgroup_name"] = target.workgroup_name
        if result.truncated:
            metadata["truncated"] = result.truncated
        envelope = DataEnvelope(
            content_type="application/json",
            data=rows.envelope_batch(result.columns, batch),
            source_id=f"redshift://{where}/{target.database}/{label}#batch={index}",
            metadata=metadata,
        )
        del batch
        yield envelope
