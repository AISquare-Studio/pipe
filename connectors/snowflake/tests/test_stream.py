"""A result's body is read a row at a time (#46 review seq 14088 M2).

Snowflake decides how large a result partition is, so no partition, the
first one (inside the status answer) included, is parsed whole: the body is
streamed and split row by row (aisquare_pipe_snowflake.stream). These tests
cover the reader on its own (every chunk size, bodies that aren't the SQL
API's), and the client and source around it: a row over ROW_BYTES is a 422,
a body that breaks off or isn't JSON a 502, and rows that come before the
columns are read again from partition 0. The memory it saves is measured in
tests/test_memory.py.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_snowflake import SnowflakeQuerySource, SnowflakeToolkit
from aisquare_pipe_snowflake import stream as stream_module
from aisquare_pipe_snowflake.client import RowTooLarge
from aisquare_pipe_snowflake.stream import BadBody, Body, RowOverCap
from tests.fakes import Canned, FakeResponse, FakeSnowflake, blob, col, real_session

SQL = "SELECT ID, NOTE FROM T"
COLUMNS = [col("ID", "fixed"), col("NOTE", "text")]
#: In a row's value: must never reach an error message.
MARK = "PRIVATE_NOTE_4242"


def _read(body: bytes, chunk: int) -> tuple[dict[str, Any], list[Any]]:
    reader = Body(iter([body[i : i + chunk] for i in range(0, len(body), chunk)]))
    head: dict[str, Any] = {}
    rows: list[Any] = []
    for key in reader.members():
        if key == "data":
            rows.extend(reader.rows())
        else:
            head[key] = reader.value()
    return head, rows


# -- the reader ------------------------------------------------------------------------

ROWS = [
    ["1", None, "a]b[c", 'q"\\x', "é😀", ""],
    [None],
    [],
    ["]["],
    ["12345678901234567890"],
    ['{"a": [1, 2]}'],  # a VARIANT: JSON text in a string
]
ANSWER = {
    "resultSetMetaData": {
        "numRows": 6,
        "rowType": [{"name": "É", "type": "text"}],
        "partitionInfo": [{"rowCount": 6, "uncompressedSize": 200}],
    },
    "data": ROWS,
    "code": "090001",
    "statementHandle": "01b2-c3",
    "createdOn": 1620151693299,
    "ok": True,
}


@pytest.mark.parametrize("ascii_only", [True, False])
@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("chunk", [*range(1, 24), 64, 1_000, 1 << 20])
def test_every_chunking_reads_the_same_rows_and_keys(ascii_only, indent, chunk):
    body = json.dumps(ANSWER, ensure_ascii=ascii_only, indent=indent).encode()
    head, rows = _read(body, chunk)
    assert rows == ROWS
    assert head == {k: v for k, v in ANSWER.items() if k != "data"}


def test_data_null_and_no_data_are_no_rows():
    assert _read(b'{"data": null, "code": "1"}', 3) == ({"code": "1"}, [])
    assert _read(b'{"code": "1"}', 2) == ({"code": "1"}, [])


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[1]",
        b'{"code": "1"',
        b'{"code": 1 2}',
        b'{"a": 1,}',
        b'{"data": [["a" "b"]]}',
        b'{"data": [["a"],]}',
        b'{"data": [,["a"]]}',
        b'{"data": [["a"]',
        b'{"data": [["a"]]} trailing',
        b'{"data": [[1, [2]]]}',  # jsonv2 never nests
        b'{"data": [["unterminated]]}',
        b'{"data": {"a": 1}}',
        b"{1: 2}",
    ],
)
@pytest.mark.parametrize("chunk", [1, 2, 5, 100])
def test_a_body_that_is_not_the_sql_api_s_is_bad(body, chunk):
    with pytest.raises(BadBody) as raised:
        _read(body, chunk)
    assert MARK not in str(raised.value)


@pytest.mark.parametrize("extra", [-6, -5, 0, 1, 10, 1_000_000])
@pytest.mark.parametrize("chunk", [64 * 1024, 1_000_003, 4 << 20])
def test_a_row_over_row_bytes_is_refused_however_it_is_chunked(
    monkeypatch, extra, chunk
):
    monkeypatch.setattr(stream_module, "ROW_BYTES", 256 * 1024)
    size = stream_module.ROW_BYTES + extra
    row = b'["' + b"x" * (size - 4) + b'"]'
    body = b'{"data": [["a"],' + row + b"]}"
    if size <= stream_module.ROW_BYTES:
        assert len(_read(body, chunk)[1]) == 2
    else:
        with pytest.raises(RowOverCap):
            _read(body, chunk)


def test_head_keys_are_bounded(monkeypatch):
    monkeypatch.setattr(stream_module, "HEAD_BYTES", 1_000)
    body = json.dumps({"message": "m" * 2_000, "data": []}).encode()
    with pytest.raises(BadBody, match="over 1000 bytes"):
        _read(body, 64)


def test_a_long_row_costs_one_pass_per_doubling_not_one_per_chunk():
    """A 4 MiB row in 1 KiB chunks: the buffer doubles, so the row is
    scanned about log2(4 MiB / 64 KiB) times, not 4,096."""
    calls = {"n": 0}
    original = stream_module._ROW

    class Counting:
        def match(self, *args: Any) -> Any:
            calls["n"] += 1
            return original.match(*args)

    body = json.dumps({"data": [["y" * (4 << 20)]]}).encode()
    stream_module._ROW = Counting()  # type: ignore[assignment]
    try:
        assert len(_read(body, 1024)[1]) == 1
    finally:
        stream_module._ROW = original
    assert calls["n"] <= 12


# -- the client and the source ----------------------------------------------------------


def _fake(rows: list[list[Any]], **canned: Any) -> FakeSnowflake:
    fake = FakeSnowflake()
    fake.add(SQL, Canned(columns=COLUMNS, rows=rows, **canned))
    return fake


def _answer_sql(fake: FakeSnowflake, change: Any) -> None:
    """``change(body)`` makes the SQL statement's result answer (a 200's
    body, a dict) into the FakeResponse to send instead. The source's own
    statements (INFORMATION_SCHEMA, ...) are answered as they were."""
    canned = fake.canned[SQL]
    original = fake._poll

    def poll(handle: str) -> FakeResponse:
        answer = original(handle)
        state = fake._statements.get(handle) or {}
        if answer.status_code != 200 or state.get("canned") is not canned:
            return answer
        return change(json.loads(answer.content))

    fake._poll = poll  # type: ignore[method-assign]


def _pull(fake: Any, **params: Any) -> list:
    params = params or {"sql": SQL}
    return list(SnowflakeQuerySource(session=fake).pull(blob(), PullParams(params)))


def _ids(envelopes: list) -> list[int]:
    return [r["ID"] for e in envelopes for r in json.loads(e.data)["rows"]]


def test_a_row_over_row_bytes_is_a_422_with_its_index_and_no_values(monkeypatch):
    monkeypatch.setattr(stream_module, "ROW_BYTES", 64 * 1024)
    rows = [[str(i), MARK] for i in range(7)] + [["7", MARK * 10_000], ["8", "n"]]
    fake = _fake(rows, partition_rows=3)
    with pytest.raises(RowTooLarge) as raised:
        _pull(fake)
    error = raised.value
    assert isinstance(error, ValueError) and error.__cause__ is None
    assert host_answer(error).status == 422
    assert error.row == 7  # in partition 2, after 7 rows
    text = str(error)
    assert "row 7 of the result is too large to read" in text
    assert f"over {stream_module.ROW_BYTES:,} bytes of JSON" in text
    assert MARK not in text and MARK not in repr(error)


def test_the_toolkit_answers_a_row_over_row_bytes_as_invalid_arguments(monkeypatch):
    monkeypatch.setattr(stream_module, "ROW_BYTES", 64 * 1024)
    fake = _fake([["1", MARK * 10_000]])
    result = SnowflakeToolkit(session=fake).call_tool(
        "SNOWFLAKE_RUN_SELECT", {"statement": SQL}, blob()
    )
    assert not result.successful and result.error_code == "invalid_arguments"
    assert "too large to read" in (result.error or "")
    assert MARK not in (result.error or "")


@pytest.mark.parametrize(
    "content",
    [
        b'{"code": "090001", "data": [["1", "a"], ["2"',  # ends early
        b'{"resultSetMetaData": {}, "data": [[["nested"]]]}',
        b"<html>maintenance</html>",
    ],
)
def test_a_result_body_that_is_not_the_sql_api_s_is_a_502(content):
    fake = _fake([["1", "a"]])
    _answer_sql(fake, lambda body: FakeResponse(200, None, content=content))
    with pytest.raises(PipelineError) as raised:
        _pull(fake)
    assert type(raised.value) is PipelineError
    assert host_answer(raised.value).status == 502
    assert "body the connector can't read" in str(raised.value)
    assert "maintenance" not in str(raised.value)


def test_rows_before_the_columns_are_read_again_from_partition_0():
    """An answer whose data comes before resultSetMetaData: its rows are read
    through (kept none) and partition 0 asked for again once the columns
    are known."""
    fake = _fake([[str(i), "n"] for i in range(5)], partition_rows=2)
    _answer_sql(
        fake, lambda body: FakeResponse(200, {"data": body.pop("data"), **body})
    )
    envelopes = _pull(fake)
    assert _ids(envelopes) == [0, 1, 2, 3, 4]
    partitions = [
        r["params"]["partition"]
        for r in fake.api_calls()
        if r["method"] == "GET" and "partition" in r["params"]
    ]
    assert partitions == [0, 1, 2]


def test_the_handle_after_the_rows_is_read_when_a_cap_cuts_them():
    """The statement's handle (its query id) comes after data, and a POST
    that answered 200 at once named none: the rest of partition 0 is read
    through to it (kept none)."""
    fake = _fake([[str(i), "n"] for i in range(20)])
    original = fake._poll

    def handle_last(handle: str) -> FakeResponse:
        body = json.loads(original(handle).content)
        meta, data = body.pop("resultSetMetaData"), body.pop("data")
        return FakeResponse(200, {"resultSetMetaData": meta, "data": data, **body})

    def submitted(body: Any, params: Any) -> FakeResponse:
        answer = FakeSnowflake._submit(fake, body, params)
        if body["statement"] != SQL:
            return answer  # the source's own statements: as they were
        return handle_last(answer.body["statementHandle"])

    fake._submit = submitted  # type: ignore[method-assign]
    envelopes = _pull(fake, sql=SQL, max_rows=3)
    assert _ids(envelopes) == [0, 1, 2]
    (handle,) = [
        h
        for h, state in fake._statements.items()
        if state["canned"] is fake.canned[SQL]
    ]
    assert {e.metadata["query_id"] for e in envelopes} == {handle}


@pytest.mark.parametrize(
    ("error", "code", "says"),
    [
        (
            ConnectionResetError("reset"),
            "upstream_error",
            "could not complete the request",
        ),
        (TimeoutError("timed out"), "timeout", "did not answer in time"),
    ],
)
def test_a_partition_that_breaks_off_midway_is_a_502(error, code, says):
    """Partition 1 breaks off after 300 bytes, inside its rows."""
    fake = _fake([[str(i), "n" * 50] for i in range(40)], partition_rows=10)
    session = real_session(
        fake,
        body_break=error,
        break_when=lambda url, params: params.get("partition") == "1",
        break_after=300,
    )
    with pytest.raises(PipelineError) as raised:
        list(
            SnowflakeQuerySource(session=session).pull(blob(), PullParams({"sql": SQL}))
        )
    assert type(raised.value) is PipelineError
    assert host_answer(raised.value).status == 502
    assert says in str(raised.value)
    result = SnowflakeToolkit(
        session=real_session(
            _fake([[str(i), "n" * 50] for i in range(40)], partition_rows=10),
            body_break=error,
            break_when=lambda url, params: params.get("partition") == "1",
            break_after=300,
        )
    ).call_tool("SNOWFLAKE_RUN_SELECT", {"statement": SQL}, blob())
    assert not result.successful and result.error_code == code, result.error


def test_rows_stream_through_a_real_session_with_gzip():
    """Snowflake gzips later partitions; requests' iter_content gives the
    decoded bytes."""
    fake = _fake([[str(i), "é😀" * 20] for i in range(300)], partition_rows=70)
    session = real_session(fake, gzip=True)
    envelopes = list(
        SnowflakeQuerySource(session=session).pull(blob(), PullParams({"sql": SQL}))
    )
    assert _ids(envelopes) == list(range(300))
