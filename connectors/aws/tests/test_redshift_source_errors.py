"""How redshift-query-source fails, as pipe-as-service answers it (#46; review
seq 13894 m5): 502 upstream_error (a PipelineError) for timeouts, missing
grants, aborted statements and what AWS refused or failed; 422 (a ValueError)
for what the caller can fix. Never a bare PipeError (500, with the database's
error text, which can echo the caller's values, in the host's traceback log).
Data API: the repo's Stubber fake; STS: moto."""

from __future__ import annotations

from typing import Any

import pytest
from aisquare.pipe import PullParams
from aisquare.pipe.errors import ConfigValidationError, PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_aws import connector as aws_connector
from aisquare_pipe_aws.connector import AwsRequestError
from aisquare_pipe_aws.credentials import AwsAccessError, AwsNotConfiguredError
from aisquare_pipe_aws.redshift import RedshiftRequestError
from aisquare_pipe_aws.redshift.config import RedshiftNotConfiguredError
from aisquare_pipe_aws.redshift.connector import RedshiftQuerySource
from aisquare_pipe_aws.redshift.sqlguard import NotReadOnly
from tests import redshift_fakes as fakes
from tests.redshift_fakes import STATEMENT_ID, blob

pytestmark = pytest.mark.usefixtures("aws", "clock")

SQL = "SELECT id, title, active FROM policies.rules"
#: In the database's or AWS's error text: never in the source's error.
LEAK = "123-45-6789"


def pull(
    params: dict[str, Any] | None = None, config: dict[str, Any] | None = None
) -> list:
    source = RedshiftQuerySource()
    return list(
        source.pull(
            blob() if config is None else config, PullParams(params or {"sql": SQL})
        )
    )


def _quiet(e: BaseException) -> None:
    """No chained exception (with its text) in a host's traceback."""
    assert e.__cause__ is None and e.__suppress_context__


# A statement that ran and failed: (status, the database's error) -> the
# exception, the host's status, words the message carries.
FAILED = [
    (
        "FAILED",
        f'ERROR: invalid input syntax for integer: "{LEAK}"',
        RedshiftRequestError,
        422,
        "(invalid_arguments)",
    ),
    (
        "FAILED",
        f'ERROR: relation "policies.{LEAK}" does not exist',
        RedshiftRequestError,
        422,
        "(not_found)",
    ),
    (
        "FAILED",
        f"ERROR: permission denied for relation {LEAK}",
        PipelineError,
        502,
        "lacks a grant",
    ),
    ("ABORTED", f"Query aborted {LEAK}", PipelineError, 502, "aborted"),
]


@pytest.mark.parametrize(("status", "error", "kind", "answer", "words"), FAILED)
def test_a_statement_that_failed(data_api, status, error, kind, answer, words):
    fakes.submit(data_api, SQL)
    fakes.describe(data_api, status, Error=error)
    with pytest.raises(kind) as raised:
        pull()
    e = raised.value
    assert type(e) is kind  # PipelineError itself: the host maps the exact type
    assert host_answer(e).status == answer
    assert words in str(e)
    assert LEAK not in str(e) and "ERROR:" not in str(e)
    _quiet(e)


def test_the_deadline_cancelled_is_a_502(data_api, clock):
    fakes.submit(data_api, SQL)
    for _ in range(40):
        fakes.describe(data_api, "STARTED")
    data_api.add("cancel_statement", {"Status": True}, {"Id": STATEMENT_ID})
    with pytest.raises(PipelineError) as raised:
        pull({"sql": SQL, "timeout_seconds": 5})
    assert type(raised.value) is PipelineError
    assert host_answer(raised.value).status == 502
    assert "the statement was cancelled" in str(raised.value)
    _quiet(raised.value)


def test_an_execute_statement_that_times_out_is_a_502(data_api):
    data_api.latencies["ExecuteStatement"] = 100.0  # past any client's timeouts
    fakes.submit(data_api, SQL)
    with pytest.raises(PipelineError) as raised:
        pull({"sql": SQL, "timeout_seconds": 5})
    assert host_answer(raised.value).status == 502
    assert "deadline" in str(raised.value)


@pytest.mark.parametrize(
    ("method", "code", "status", "answer"),
    [
        ("execute_statement", "AccessDeniedException", 403, 502),
        ("execute_statement", "ThrottlingException", 400, 502),
        ("execute_statement", "ActiveStatementsExceededException", 400, 502),
        ("execute_statement", "InternalServerException", 500, 502),
        ("execute_statement", "ExpiredTokenException", 400, 502),
        ("execute_statement", "ValidationException", 400, 422),
        ("execute_statement", "ResourceNotFoundException", 404, 422),
    ],
)
def test_aws_refusals(data_api, method, code, status, answer):
    data_api.error(method, code, status)
    with pytest.raises((PipelineError, AwsRequestError)) as raised:
        pull()
    e = raised.value
    assert host_answer(e).status == answer
    assert type(e) is (PipelineError if answer == 502 else AwsRequestError)
    assert "redshift-data:ExecuteStatement" in str(e) and code in str(e)
    assert "secret-looking detail" not in str(e)  # AWS's message (the fake's)
    _quiet(e)


def test_a_refused_poll_is_a_502_and_cancels(data_api):
    fakes.submit(data_api, SQL)
    data_api.error("describe_statement", "ThrottlingException", 400)
    data_api.add("cancel_statement", {"Status": True}, {"Id": STATEMENT_ID})
    with pytest.raises(PipelineError) as raised:
        pull()
    assert host_answer(raised.value).status == 502
    assert data_api.operations()[-1] == "CancelStatement"


def test_a_refused_page_is_a_502(data_api):
    fakes.submit(data_api, SQL)
    fakes.describe(data_api, "FINISHED", ResultRows=1)
    data_api.error("get_statement_result", "InternalServerException", 500)
    with pytest.raises(PipelineError) as raised:
        pull()
    assert host_answer(raised.value).status == 502
    assert "GetStatementResult" in str(raised.value)


def test_a_refused_assume_role_is_a_502(monkeypatch, data_api):
    def refused(cfg: Any, *, base_session: Any = None) -> Any:
        raise AwsAccessError(
            "AWS refused sts:AssumeRole: the customer role does not trust this "
            "service's principal",
            code="forbidden",
            aws_code="AccessDenied",
        )

    monkeypatch.setattr(aws_connector, "assume_role_session", refused)
    with pytest.raises(PipelineError) as raised:
        pull()
    assert host_answer(raised.value).status == 502
    assert "sts:AssumeRole" in str(raised.value)


@pytest.mark.parametrize(
    ("config", "params", "kind"),
    [
        ({}, {"sql": SQL}, AwsNotConfiguredError),  # the AWS part, first
        (blob(database=None), {"sql": SQL}, RedshiftNotConfiguredError),
        (None, {"sql": "DELETE FROM policies.rules"}, NotReadOnly),
        (None, {"sql": SQL, "batch_size": 0}, ConfigValidationError),
        (None, {"sql": SQL, "cluster_identifier": "other"}, ConfigValidationError),
        (None, {"table": "not a table"}, ConfigValidationError),
        (None, {"sql": SQL, "nope": 1}, ConfigValidationError),
    ],
)
def test_what_is_refused_before_aws_is_a_422(
    data_api, assume_calls, config, params, kind
):
    with pytest.raises(kind) as raised:
        pull(params, config)
    assert host_answer(raised.value).status == 422
    assert data_api.calls == [] and assume_calls == []


def test_params_that_are_not_a_mapping_are_a_422(data_api):
    with pytest.raises(ConfigValidationError) as raised:
        list(RedshiftQuerySource().pull(blob(), ["sql"]))  # type: ignore[arg-type]
    assert host_answer(raised.value).status == 422


def test_the_toolkit_still_shows_the_database_s_own_error(data_api):
    """The toolkit answers the caller directly, so it keeps the database's
    text (it says what to fix); only the source leaves it out."""
    from aisquare_pipe_aws.redshift import RedshiftToolkit

    fakes.submit(data_api, SQL)
    fakes.describe(data_api, "FAILED", Error='ERROR: column "titel" does not exist')
    result = RedshiftToolkit().call_tool("REDSHIFT_RUN_SELECT", {"sql": SQL}, blob())
    assert result.error_code == "not_found" and "titel" in (result.error or "")
