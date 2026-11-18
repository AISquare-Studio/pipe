"""How the source fails, as pipe-as-service answers it: 502 upstream_error
(a PipelineError, Retry-After when Snowflake gave one) for upstream and
transient failures, 422 (a ValueError) for what the caller can fix. Never a
bare Exception (500, with Snowflake's text in the host's log), and never
Snowflake's own error text in the message (the #37 review's m2)."""

from __future__ import annotations

import pytest
import requests
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_snowflake import SnowflakeQuerySource
from aisquare_pipe_snowflake.toolkit import SnowflakeRequestError
from tests.fakes import Canned, FakeResponse, FakeSnowflake, blob, col

#: In every faked Snowflake error text: it must never reach the source's error.
LEAK = "SECRET_PAYROLL_XYZ"
SQL = "SELECT ID FROM T"


def _fake(error: tuple[int, str, str, str] | None = None) -> FakeSnowflake:
    fake = FakeSnowflake()
    fake.add(SQL, Canned(columns=[col("ID", "fixed")], rows=[["1"]], error=error))
    return fake


def _pull(fake: FakeSnowflake, **params) -> list:
    params = params or {"sql": SQL}
    return list(SnowflakeQuerySource(session=fake).pull(blob(), PullParams(params)))


# The statement fails: (status, code, sqlState, Snowflake's message), the words
# the 422 carries.
CALLER = [
    (
        (
            422,
            "001003",
            "42000",
            (
                f"SQL compilation error: syntax error line 1 at "
                f"position 7 unexpected '{LEAK}'."
            ),
        ),
        "at line 1, position 7",
    ),
    (
        (422, "002003", "42S02", f"Object '{LEAK}' does not exist or not authorized."),
        "does not exist, or role",
    ),
    (
        (422, "100038", "22018", f"Numeric value '{LEAK}' is not recognized"),
        "422, 100038",
    ),
]


@pytest.mark.parametrize(("error", "words"), CALLER)
def test_what_the_caller_can_fix_is_a_422_value_error(error, words):
    with pytest.raises(SnowflakeRequestError) as raised:
        _pull(_fake(error))
    e = raised.value
    assert isinstance(e, ValueError)
    assert host_answer(e).status == 422
    assert (e.status, e.code) == (error[0], error[1])
    assert words in str(e)
    assert LEAK not in str(e)


# (status, code, sqlState, message) -> words the 502 carries
UPSTREAM_STATEMENT = [
    ((422, "003001", "42501", f"Insufficient privileges on {LEAK}"), "lacks a grant"),
    ((422, "000606", "57P03", f"No active warehouse {LEAK}"), "cannot use warehouse"),
    ((422, "000630", "57014", f"Statement reached its timeout {LEAK}"), "000630"),
]


@pytest.mark.parametrize(("error", "words"), UPSTREAM_STATEMENT)
def test_grants_warehouses_and_timeouts_are_a_502(error, words):
    with pytest.raises(PipelineError) as raised:
        _pull(_fake(error))
    e = raised.value
    assert type(e) is PipelineError  # the host maps the exact type
    assert host_answer(e).status == 502
    assert words in str(e)
    assert LEAK not in str(e)
    assert e.__cause__ is None and e.__suppress_context__


@pytest.mark.parametrize(
    ("status", "headers", "retry_after", "words"),
    [
        (503, {"Retry-After": "5"}, 5.0, "503"),
        (500, {}, None, "500"),
        (401, {}, None, "credential must be fixed"),
        (403, {}, None, "lacks a grant"),
        (408, {}, None, "reached its timeout"),
    ],
)
def test_upstream_http_failures_are_a_502(status, headers, retry_after, words):
    # No table in it, so no marker lookup comes first (that one is best effort).
    fake = _fake()
    fake.add("SELECT 1", Canned(columns=[col("1", "fixed")], rows=[["1"]]))
    fake.override(
        "submit",
        FakeResponse(
            status, {"code": "390000", "message": f"failed {LEAK}"}, headers=headers
        ),
    )
    with pytest.raises(PipelineError) as raised:
        _pull(fake, sql="SELECT 1")
    e = raised.value
    assert host_answer(e).status == 502
    assert e.retry_after == retry_after
    assert words in str(e)
    assert LEAK not in str(e)


def test_a_timeout_is_a_502():
    fake = _fake()
    original = fake.request

    def request(method, url, **kwargs):
        if "/api/v2/statements" in url:
            raise requests.ReadTimeout(f"timed out {LEAK}")
        return original(method, url, **kwargs)

    fake.request = request  # type: ignore[method-assign]
    with pytest.raises(PipelineError) as raised:
        _pull(fake)
    assert host_answer(raised.value).status == 502
    assert LEAK not in str(raised.value)


@pytest.mark.parametrize(
    "params",
    [
        {"sql": "DELETE FROM T"},  # NotReadOnly
        {"sql": SQL, "batch_size": 0},  # a bad param
        {"table": "T", "sql": SQL},  # both
        {"role": "ACCOUNTADMIN", "sql": SQL},  # server-controlled
        {"nope": 1, "sql": SQL},  # unknown
        {"table": "a.b.c.d"},  # a bad reference
    ],
)
def test_refused_params_are_a_422(params):
    fake = _fake()
    with pytest.raises(ValueError) as raised:
        _pull(fake, **params)
    assert host_answer(raised.value).status == 422
    assert fake.submits() == []


def test_params_that_are_not_a_mapping_are_a_422():
    with pytest.raises(ValueError) as raised:
        list(SnowflakeQuerySource(session=_fake()).pull(blob(), ["sql"]))  # type: ignore[arg-type]
    assert host_answer(raised.value).status == 422


def test_a_missing_credential_is_a_422():
    fake = _fake()
    with pytest.raises(ValueError) as raised:
        list(SnowflakeQuerySource(session=fake).pull({}, PullParams({"sql": SQL})))
    assert host_answer(raised.value).status == 422
    assert fake.requests == []
