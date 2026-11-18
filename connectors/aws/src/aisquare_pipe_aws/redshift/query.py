"""Run one checked SELECT and collect its rows, within caps and a deadline.

:func:`run_select` is the only path that sends SQL. It checks the statement
(:func:`~aisquare_pipe_aws.redshift.sqlguard.check_select_only`) FIRST, and only
then opens the session (the AssumeRole) and submits it, so text that is not
one read-only query never reaches AWS, not even STS.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aisquare_pipe_aws.redshift import dataapi, rows
from aisquare_pipe_aws.redshift.config import RedshiftTarget
from aisquare_pipe_aws.redshift.sqlguard import check_select_only

TRUNCATED_ROWS = "max_rows"
TRUNCATED_BYTES = "max_result_bytes"


@dataclass
class Result:
    statement_id: str
    columns: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    #: Rows the statement returned in all (DescribeStatement's ResultRows,
    #: else GetStatementResult's TotalNumRows), or None when unknown.
    total_rows: int | None
    #: Why fewer rows came back than the statement returned, else None.
    truncated: str | None
    result_bytes: int
    duration_ms: int | None
    #: Read with ``encoded=True``: each row as the source's envelopes carry
    #: it (:func:`~aisquare_pipe_aws.redshift.rows.envelope_json`) and its
    #: :func:`~aisquare_pipe_aws.redshift.rows.row_digest`, instead of
    #: ``rows`` (about a fifth of the memory row objects take).
    encoded: list[bytes] | None = None
    digests: list[str] | None = None

    @property
    def count(self) -> int:
        """The rows read, as objects or encoded."""
        return len(self.rows) if self.encoded is None else len(self.encoded)

    @property
    def result_sha256(self) -> str:
        if self.digests is not None:
            return rows.result_sha256_of_digests(self.columns, self.digests)
        return rows.result_sha256(self.columns, self.rows)


def run_select(
    sql: str,
    *,
    open_session: Callable[[], Any],
    target: RedshiftTarget,
    deadline: dataapi.Deadline,
    max_rows: int,
    max_result_bytes: int,
    encoded: bool = False,
) -> Result:
    """Check ``sql``, then run it and read at most ``max_rows`` rows and
    ``max_result_bytes`` bytes of JSON rows (``encoded``: kept as JSON
    bytes and digests, :class:`Result`). One parsed page is held at a time,
    and its rows are converted one by one. Raises
    :class:`~aisquare_pipe_aws.redshift.sqlguard.NotReadOnly` before any AWS
    call; :class:`~aisquare_pipe_aws.redshift.dataapi.StatementTimeout`,
    :class:`~aisquare_pipe_aws.redshift.dataapi.StatementFailed` and
    :class:`~aisquare_pipe_aws.credentials.AwsAccessError` after."""
    statement_sql = check_select_only(sql)
    session = open_session()
    api = dataapi.client(session, target, deadline)
    statement = dataapi.execute(api, target, statement_sql)
    cols: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    as_json: list[bytes] | None = [] if encoded else None
    digests: list[str] | None = [] if encoded else None
    count = 0
    size = 0
    total = statement.result_rows
    truncated: str | None = None
    for page in dataapi.result_pages(api, statement):
        if not cols:
            cols = rows.columns(page.get("ColumnMetadata"))
        if total is None and isinstance(page.get("TotalNumRows"), int):
            total = page["TotalNumRows"]
        for row in rows.iter_page(cols, page.get("Records")):
            if count >= max_rows:
                truncated = TRUNCATED_ROWS
                break
            stable = rows.canonical(row)
            row_bytes = len(stable)  # rows.json_size, without a second dump
            if size + row_bytes > max_result_bytes:
                truncated = TRUNCATED_BYTES
                break
            if as_json is None or digests is None:
                out.append(row)
            else:
                as_json.append(rows.envelope_json(row))
                digests.append(hashlib.sha256(stable).hexdigest())
            count += 1
            size += row_bytes
        if truncated is not None:
            break
        if count >= max_rows and page.get("NextToken"):
            truncated = TRUNCATED_ROWS
            break
        del page  # let go before the generator reads the next page
    duration = statement.duration_ns
    return Result(
        statement_id=statement.id,
        columns=cols,
        rows=out,
        total_rows=total,
        truncated=truncated,
        result_bytes=size,
        duration_ms=duration // 1_000_000 if duration is not None else None,
        encoded=as_json,
        digests=digests,
    )
