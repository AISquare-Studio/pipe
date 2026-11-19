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


def _passed(
    body: bytes, chunk: int, take: int
) -> tuple[dict[str, Any], list[Any], int]:
    """(the keys, the rows taken, the most bytes buffered): ``take`` rows of
    data through :meth:`Body.rows`, then the rest through ``pass_rows``."""
    chunks = iter([body[i : i + chunk] for i in range(0, len(body), chunk)])
    reader = Body(chunks)
    most = {"n": 0}
    fill = reader._fill

    def watched(want: int) -> bool:
        more = fill(want)
        most["n"] = max(most["n"], len(reader._buf))
        return more

    reader._fill = watched  # type: ignore[method-assign]
    head: dict[str, Any] = {}
    taken: list[Any] = []
    for key in reader.members():
        if key == "data":
            rows = reader.rows()
            for _, row in zip(range(take), rows, strict=False):
                taken.append(row)
            reader.pass_rows()
        else:
            head[key] = reader.value()
    return head, taken, most["n"]


@pytest.mark.parametrize("take", [0, 1, 2, 6])
@pytest.mark.parametrize("chunk", [*range(1, 12), 64, 1 << 20])
def test_pass_rows_reads_on_to_the_next_key_however_it_is_chunked(take, chunk):
    """Brackets and escaped quotes inside strings, split across chunks, and
    from data's start or after rows taken: the keys after data read the
    same."""
    for indent in (None, 2):
        body = json.dumps(ANSWER, indent=indent).encode()
        head, taken, _ = _passed(body, chunk, take)
        assert taken == ROWS[:take]
        assert head == {k: v for k, v in ANSWER.items() if k != "data"}


def test_pass_rows_reads_past_a_row_over_row_bytes_holding_about_a_chunk(monkeypatch):
    """#46 review seq 14138 nit d: rows passed through meet no row cap (the
    caller gets none of them), and are let go as they arrive."""
    monkeypatch.setattr(stream_module, "ROW_BYTES", 64 * 1024)
    big = MARK + '\\"]["' + "x" * (stream_module.ROW_BYTES * 4)
    body = json.dumps(
        {"data": [["0", "a"], ["1", big], ["2", "c"]], "code": "1"}
    ).encode()
    with pytest.raises(RowOverCap):
        _read(body, stream_module.READ_CHUNK)
    head, taken, most = _passed(body, stream_module.READ_CHUNK, 1)
    assert (head, taken) == ({"code": "1"}, [["0", "a"]])
    assert most <= 2 * stream_module.READ_CHUNK + 1


@pytest.mark.parametrize(
    "body",
    [
        b'{"data": [["a"], ["b"',
        b'{"data": [["a"], ["b\\"]]}',  # an escaped quote: the string goes on
        b'{"data": {"a": 1}}',
    ],
)
@pytest.mark.parametrize("take", [0, 1])
def test_pass_rows_refuses_a_body_that_ends_inside_data(body, take):
    with pytest.raises(BadBody):
        _passed(body, 2, take)


def _chunked(body: bytes, chunk: int) -> Body:
    return Body(iter([body[i : i + chunk] for i in range(0, len(body), chunk)]))


@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("chunk", [*range(1, 12), 64, 1 << 20])
def test_row_follows_says_whether_a_row_follows_before_and_between_rows(indent, chunk):
    """#51 review seq 14199 n2: Body.row_follows, asked before rows() begins
    and after each row it yields, however the body is chunked; rows() reads
    on from where it left off, and the keys after data read the same."""
    reader = _chunked(json.dumps(ANSWER, indent=indent).encode(), chunk)
    head: dict[str, Any] = {}
    taken: list[Any] = []
    for key in reader.members():
        if key != "data":
            head[key] = reader.value()
            continue
        rows = reader.rows()
        for _ in ROWS:
            assert reader.row_follows() is True
            assert reader.row_follows() is True  # asking again reads nothing
            taken.append(next(rows))
        assert reader.row_follows() is False
        assert next(rows, None) is None
    assert taken == ROWS
    assert head == {k: v for k, v in ANSWER.items() if k != "data"}


@pytest.mark.parametrize("data", [[], None])
@pytest.mark.parametrize("chunk", [1, 2, 64])
def test_row_follows_is_false_for_data_without_rows(data, chunk):
    reader = _chunked(json.dumps({"data": data, "code": "1"}).encode(), chunk)
    head: dict[str, Any] = {}
    for key in reader.members():
        if key == "data":
            rows = reader.rows()
            assert reader.row_follows() is False
            assert list(rows) == []
        else:
            head[key] = reader.value()
    assert head == {"code": "1"}


@pytest.mark.parametrize("take", [0, 1])
@pytest.mark.parametrize("chunk", [1, 7, 64 * 1024])
def test_row_follows_reads_none_of_a_row_over_row_bytes(take, chunk, monkeypatch):
    """The row that follows is over ROW_BYTES: row_follows says it is there,
    without reading it (no RowOverCap), and pass_rows reads on past it to
    the next key."""
    monkeypatch.setattr(stream_module, "ROW_BYTES", 64 * 1024)
    big = ["1", MARK * 10_000]
    rows_in = [["0", "a"], big] if take else [big, ["0", "a"]]
    body = json.dumps({"data": rows_in, "code": "1"}).encode()
    with pytest.raises(RowOverCap):
        _read(body, chunk)
    reader = _chunked(body, chunk)
    head: dict[str, Any] = {}
    for key in reader.members():
        if key == "data":
            rows = reader.rows()
            assert [next(rows) for _ in range(take)] == rows_in[:take]
            assert reader.row_follows() is True
            assert reader.rows_read == take
            reader.pass_rows()
        else:
            head[key] = reader.value()
    assert head == {"code": "1"}


@pytest.mark.parametrize("body", [b'{"data": [', b'{"data": [["a"]  '])
def test_row_follows_refuses_a_body_that_ends_inside_data(body):
    reader = _chunked(body, 2)
    assert next(reader.members()) == "data"
    rows = reader.rows()
    if body.endswith(b"  "):
        assert next(rows) == ["a"]
    with pytest.raises(BadBody):
        reader.row_follows()


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


def _handle_after_the_rows(fake: FakeSnowflake) -> None:
    """The SQL statement's POST answers 200 at once, its handle after data,
    as Snowflake orders an answer's keys."""
    original = fake._poll

    def handle_last(handle: str) -> FakeResponse:
        body = json.loads(original(handle).content)
        meta, data = body.pop("resultSetMetaData"), body.pop("data")
        return FakeResponse(200, {"resultSetMetaData": meta, "data": data, **body})

    def submitted(body: Any, params: Any) -> FakeResponse:
        answer = FakeSnowflake._submit(fake, body, params)
        if body["statement"] != SQL:
            return answer
        return handle_last(answer.body["statementHandle"])

    fake._submit = submitted  # type: ignore[method-assign]


#: A cut by max_result_bytes at row 1 (row 0 fits 400 bytes, row 1 doesn't),
#: then a row over ROW_BYTES (64 KiB here) that the caller never gets. (A
#: cut by max_rows leaves at most one row: the POST asks for
#: ROWS_PER_RESULTSET = max_rows + 1, and Snowflake, like the fake, sends no
#: more. That row is never parsed either: below.)
_CUT_THEN_OVER = [["0", "n"], ["1", "y" * 1_000], ["2", MARK * 10_000], ["3", "n"]]


def test_rows_read_through_to_the_handle_after_a_cut_meet_no_row_cap(monkeypatch):
    """#46 review seq 14138 nit d: the POST answers 200 at once, the handle
    after data, and a cap cuts partition 0's rows. Reading through the rest
    of it to the handle applied the row cap to rows the caller never gets:
    RowTooLarge (422) instead of the rows before the cut. Now 1 row,
    truncated max_result_bytes, with the statement's query_id, as through
    the 202 polls (the handle known before the rows)."""
    monkeypatch.setattr(stream_module, "ROW_BYTES", 64 * 1024)
    fake = _fake(_CUT_THEN_OVER)
    _handle_after_the_rows(fake)
    envelopes = _pull(fake, sql=SQL, max_result_bytes=400)
    assert _ids(envelopes) == [0]
    assert {e.metadata["truncated"] for e in envelopes} == {"max_result_bytes"}
    (handle, *_) = [
        h
        for h, state in fake._statements.items()
        if state["canned"] is fake.canned[SQL]
    ]
    assert {e.metadata["query_id"] for e in envelopes} == {handle}
    # A row over the cap is still refused when the caller would get it.
    with pytest.raises(RowTooLarge) as raised:
        _pull(fake, sql=SQL)
    assert raised.value.row == 2


def test_rows_before_the_columns_meet_no_row_cap_until_they_are_read_again(
    monkeypatch,
):
    """The same for rows that come before the columns: they are read
    through (kept none) and partition 0 read again; only the rows that read
    takes meet the row cap. A cut at row 1 and a row over ROW_BYTES at row
    2: RowTooLarge before, 1 row now."""
    monkeypatch.setattr(stream_module, "ROW_BYTES", 64 * 1024)
    fake = _fake(_CUT_THEN_OVER)
    _answer_sql(
        fake, lambda body: FakeResponse(200, {"data": body.pop("data"), **body})
    )
    envelopes = _pull(fake, sql=SQL, max_result_bytes=400)
    assert _ids(envelopes) == [0]
    assert {e.metadata["truncated"] for e in envelopes} == {"max_result_bytes"}
    with pytest.raises(RowTooLarge) as raised:
        _pull(fake, sql=SQL)
    assert raised.value.row == 2


def _sql_handle(fake: FakeSnowflake) -> str:
    (handle,) = [
        h
        for h, state in fake._statements.items()
        if state["canned"] is fake.canned[SQL]
    ]
    return handle


@pytest.mark.parametrize("path", ["202", "post-200"])
def test_the_row_past_max_rows_is_never_parsed_so_it_meets_no_row_cap(
    path, monkeypatch
):
    """#51 review seq 14199 n2: at max_rows the next row (the one
    ROWS_PER_RESULTSET = max_rows + 1 asks for, so a cut shows) was parsed
    only to learn that rows remain, and so met the row cap: max_rows 1 and a
    row over ROW_BYTES at row 1 was RowTooLarge row 1, both through the 202
    polls (the handle before the rows) and from a POST answered 200 at once
    (the handle after them). Now that row is never parsed: it is only
    checked to be there, and from the POST answered 200 at once read through
    to the handle unparsed (pass_rows). 1 row, truncated max_rows, with the
    statement's query_id."""
    monkeypatch.setattr(stream_module, "ROW_BYTES", 64 * 1024)
    fake = _fake([["0", "n"], ["1", MARK * 10_000]])
    if path == "post-200":
        _handle_after_the_rows(fake)
    envelopes = _pull(fake, sql=SQL, max_rows=1)
    assert _ids(envelopes) == [0]
    assert {e.metadata["truncated"] for e in envelopes} == {"max_rows"}
    assert {e.metadata["query_id"] for e in envelopes} == {_sql_handle(fake)}
    # A whole result at max_rows is not truncated: no row follows.
    whole = _fake([["0", "n"]])
    if path == "post-200":
        _handle_after_the_rows(whole)
    envelopes = _pull(whole, sql=SQL, max_rows=1)
    assert _ids(envelopes) == [0]
    assert "truncated" not in envelopes[-1].metadata
    # A row over the cap is still refused when the caller would get it.
    with pytest.raises(RowTooLarge) as raised:
        _pull(fake, sql=SQL, max_rows=2)
    assert raised.value.row == 1


def test_the_row_past_max_rows_in_the_next_partition_is_never_parsed(monkeypatch):
    """The same when partition 0 ends at max_rows: partition 1 is asked for
    (only it can say whether rows remain), and its first row, over
    ROW_BYTES, is only checked to be there, never parsed."""
    monkeypatch.setattr(stream_module, "ROW_BYTES", 64 * 1024)
    fake = _fake([["0", "n"], ["1", "n"], ["2", MARK * 10_000]], partition_rows=2)
    envelopes = _pull(fake, sql=SQL, max_rows=2)
    assert _ids(envelopes) == [0, 1]
    assert {e.metadata["truncated"] for e in envelopes} == {"max_rows"}
    partitions = [
        r["params"]["partition"]
        for r in fake.api_calls()
        if r["method"] == "GET" and "partition" in r["params"]
    ]
    assert partitions == [1]


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
