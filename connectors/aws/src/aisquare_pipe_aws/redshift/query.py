"""Run one checked SELECT and collect its rows, within caps and a deadline.

:func:`run_select` is the only path that sends SQL. It checks the statement
(:func:`~aisquare_pipe_aws.redshift.sqlguard.check_select_only`) FIRST, and only
then opens the session (the AssumeRole) and submits it, so text that is not
one read-only query never reaches AWS, not even STS.
"""

from __future__ import annotations

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

    @property
    def result_sha256(self) -> str:
        return rows.result_sha256(self.columns, self.rows)


def run_select(
    sql: str,
    *,
    open_session: Callable[[], Any],
    target: RedshiftTarget,
    deadline: dataapi.Deadline,
    max_rows: int,
    max_result_bytes: int,
) -> Result:
    """Check ``sql``, then run it and read at most ``max_rows`` rows and
    ``max_result_bytes`` bytes of JSON rows. Raises
    :class:`~aisquare_pipe_aws.redshift.sqlguard.NotReadOnly` before any AWS
    call; :class:`~aisquare_pipe_aws.redshift.dataapi.StatementTimeout`,
    :class:`~aisquare_pipe_aws.redshift.dataapi.StatementFailed` and
    :class:`~aisquare_pipe_aws.credentials.AwsAccessError` after."""
    statement_sql = check_select_only(sql)
    session = open_session()
    api = dataapi.client(session, target, deadline)
    statement = dataapi.execute(api, target, statement_sql, deadline)
    cols: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    size = 0
    total = statement.result_rows
    truncated: str | None = None
    for page in dataapi.result_pages(api, statement, deadline):
        if not cols:
            cols = rows.columns(page.get("ColumnMetadata"))
        if total is None and isinstance(page.get("TotalNumRows"), int):
            total = page["TotalNumRows"]
        for row in rows.convert_page(cols, page.get("Records")):
            if len(out) >= max_rows:
                truncated = TRUNCATED_ROWS
                break
            row_bytes = rows.json_size(row)
            if size + row_bytes > max_result_bytes:
                truncated = TRUNCATED_BYTES
                break
            out.append(row)
            size += row_bytes
        if truncated is not None:
            break
        if len(out) >= max_rows and page.get("NextToken"):
            truncated = TRUNCATED_ROWS
            break
    duration = statement.duration_ns
    return Result(
        statement_id=statement.id,
        columns=cols,
        rows=out,
        total_rows=total,
        truncated=truncated,
        result_bytes=size,
        duration_ms=duration // 1_000_000 if duration is not None else None,
    )
