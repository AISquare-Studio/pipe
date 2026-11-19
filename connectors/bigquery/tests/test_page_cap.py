"""Every row page is read within a cap (#46 reopen seq 14031).

A page's body is streamed and its bytes and cells (``"v":`` keys) counted
before any of it is parsed. Over READ_BYTES or READ_CELLS it is dropped and
asked for again with fewer rows, down to one; one row over them is refused
(RowTooLarge, a ValueError: 422) with its size and none of its values. The
first page of a query (jobs.query's answer) is read the same way: when it is
dropped, the job it named gives the page again.

Most tests shrink the caps (``small_caps``) so the rows stay small; the
memory tests (tests/test_memory.py) run at the real ones.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_bigquery import (
    BigQueryQuerySource,
    BigQueryToolkit,
    RowTooLarge,
    query,
)
from aisquare_pipe_bigquery import client as client_module
from tests.fakes import (
    PROJECT,
    CannedQuery,
    FakeGoogle,
    FakeResponse,
    FakeTable,
    blob,
    field_,
    real_session,
)

SQL = "SELECT customer_id, ARRAY_AGG(order_id) AS orders FROM sales.o GROUP BY 1"
FIELDS = [field_("orders", "INT64", "REPEATED"), field_("customer_id", "INT64")]
#: In every array element: must never reach an error message.
MARK = 424_242_000


@pytest.fixture
def small_caps(monkeypatch):
    """Caps a few hundred elements reach: a page holds READ_CELLS = 2,000."""
    monkeypatch.setattr(query, "REPEATED_CELLS", 10)
    monkeypatch.setattr(query, "PAGE_CELLS", 1_000)
    monkeypatch.setattr(query, "READ_CELLS", 2_000)
    monkeypatch.setattr(query, "PAGE_BYTES", 64 * 1024)
    monkeypatch.setattr(query, "READ_BYTES", 128 * 1024)
    monkeypatch.setattr(client_module, "READ_CHUNK", 1024)


def _rows(lengths: list[int]) -> list[list[Any]]:
    """One row per length: ``orders`` that many elements, ``customer_id`` its
    index."""
    return [[[MARK + j for j in range(n)], i] for i, n in enumerate(lengths)]


def _query_google(lengths: list[int], **canned: Any) -> FakeGoogle:
    google = FakeGoogle()
    google.add_dataset(f"{PROJECT}.sales")
    google.add_query(SQL, CannedQuery(FIELDS, _rows(lengths), **canned))
    return google


def _table_google(lengths: list[int]) -> FakeGoogle:
    google = FakeGoogle()
    google.add_table(f"{PROJECT}.sales.o", FakeTable(FIELDS, _rows(lengths)))
    return google


def _pull(google: Any, params: dict[str, Any]) -> list:
    return list(BigQueryQuerySource(session=google).pull(blob(), PullParams(params)))


def _ids(envelopes: list) -> list[int]:
    return [r["customer_id"] for e in envelopes for r in json.loads(e.data)["rows"]]


def _row_pages(google: FakeGoogle) -> list[dict[str, Any]]:
    """The row page requests (jobs.query runs, getQueryResults, tabledata.list)."""
    return [
        c
        for c in google.api_calls()
        if c["url"].endswith("/data")
        or "/queries/" in c["url"]
        or (c["method"] == "POST" and not json.loads(c["data"]).get("dryRun"))
    ]


def _asked(google: FakeGoogle) -> list[int]:
    out = []
    for call in _row_pages(google):
        if call["method"] == "POST":
            out.append(int(json.loads(call["data"])["maxResults"]))
        else:
            out.append(int(call["params"]["maxResults"]))
    return out


# -- counting ------------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 5, 7, 64])
def test_keys_split_across_chunks_are_counted_once(chunk):
    rows = [{"f": [{"v": [{"v": "1"}, {"v": "v"}]}, {"v": {"f": [{"v": None}]}}]}] * 9
    body = json.dumps({"rows": rows}).replace(" ", "").encode()
    counts = client_module._Counts()
    for start in range(0, len(body), chunk):
        counts.add(body[start : start + chunk])
    assert counts.cells == body.count(b'"v":') == 9 * 5
    assert counts.rows == body.count(b'"f":') == 9 * 2


# -- a page over the cap is asked for again ------------------------------------------


def test_a_query_s_first_page_over_the_cap_is_read_again_from_its_job(small_caps):
    """jobs.query's answer held 90 rows of 302 cells: dropped, and the job it
    named gives the first page again with the rows that fit, from row 0."""
    google = _query_google([300] * 40)
    envelopes = _pull(google, {"sql": SQL})
    assert _ids(envelopes) == list(range(40))
    calls = _row_pages(google)
    assert calls[0]["method"] == "POST"
    again = calls[1]
    assert again["method"] == "GET" and again["url"].endswith("/queries/job_1")
    assert "pageToken" not in again["params"]
    asked = _asked(google)
    assert asked[0] == query.PAGE_CELLS // 11 and 1 <= asked[1] <= asked[0] // 2
    assert asked[1] * 302 <= query.PAGE_CELLS
    assert len(google.runs()) == 1  # the query ran once


def test_a_finished_poll_over_the_cap_is_read_again_from_the_job(small_caps):
    google = _query_google([300] * 40, polls=1)
    envelopes = _pull(google, {"sql": SQL})
    assert _ids(envelopes) == list(range(40))
    calls = _row_pages(google)
    # jobs.query (not done), the poll (done, over the cap), the page again.
    assert [c["method"] for c in calls[:3]] == ["POST", "GET", "GET"]
    assert "timeoutMs" in calls[1]["params"] and "pageToken" not in calls[2]["params"]


@pytest.mark.parametrize("path", ["sql", "table"])
def test_rows_that_grow_midway_are_asked_for_again_from_their_first_row(
    small_caps, path
):
    """[] for 100 rows, then 300 elements: the page that reaches them is
    dropped and asked for again from its first row (startIndex, documented
    for tabledata.list and getQueryResults), not with its page token sent
    again with a smaller maxResults (undocumented: review seq 14088 n2).
    Nothing skipped or repeated."""
    lengths = [0] * 100 + [300] * 30
    if path == "sql":
        google = _query_google(lengths)
        envelopes = _pull(google, {"sql": SQL})
    else:
        google = _table_google(lengths)
        envelopes = _pull(google, {"table": "sales.o"})
    assert _ids(envelopes) == list(range(130))
    params = [c["params"] for c in _row_pages(google)[1:]]
    tokens = [p["pageToken"] for p in params if "pageToken" in p]
    assert len(tokens) == len(set(tokens))  # no token sent twice
    again = [p for p in params if "startIndex" in p]
    assert again and all("pageToken" not in p for p in again)
    for p in again:
        # The dropped page's token named the same row.
        assert str(p["startIndex"]) in tokens


def test_a_page_of_wide_rows_over_the_cap_costs_one_dropped_read(small_caps):
    """300 rows of 2 KB, 1,000 asked for (by cells): the page is over
    READ_BYTES. It is asked for again with the rows that fit PAGE_BYTES at
    what was read of it, at once: halving would drop 500, 250, 125 and 62
    first (BigQuery's own page limit answers a long-row page the same way,
    as many rows as fit whatever was asked)."""
    google = _table_google([0] * 300)
    google.data[PROJECT]["sales"]["o"] = FakeTable(
        [field_("s", "STRING")], [["x" * 2_000] for _ in range(300)]
    )
    envelopes = _pull(google, {"table": "sales.o"})
    assert sum(e.metadata["row_count"] for e in envelopes) == 300
    asked = _asked(google)
    fit = query.PAGE_BYTES // 2_000  # 32
    assert asked[0] == query.PAGE_CELLS
    assert fit // 2 <= asked[1] <= fit
    assert all(n <= fit for n in asked[1:])
    # One dropped read, then pages that fit until the rows run out.
    assert len(asked) == 1 + -(-300 // asked[1])


# -- one row over the cap -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "lengths", "row"),
    [
        ("sql", [2_500, 1, 1], 0),  # jobs.query's first page
        ("sql", [3] * 50 + [2_500], 50),  # after pages that fit
        ("table", [3] * 7 + [2_500, 3], 7),
    ],
)
def test_a_row_over_the_cap_is_a_422_with_its_size_and_no_data(
    small_caps, path, lengths, row
):
    google = _query_google(lengths) if path == "sql" else _table_google(lengths)
    params = {"sql": SQL} if path == "sql" else {"table": "sales.o"}
    with pytest.raises(RowTooLarge) as raised:
        _pull(google, params)
    error = raised.value
    assert host_answer(error).status == 422
    assert isinstance(error, ValueError) and error.__cause__ is None
    assert error.row == row and error.complete
    assert error.cells == 2_502  # the orders cell, its 2,500 elements, the id
    text = str(error)
    where = "the result" if path == "sql" else "the table"
    assert f"row {row} of {where} is too large to read" in text
    assert f"{error.page_bytes:,} bytes of JSON with 2,502 values" in text
    assert f"at most {query.READ_BYTES:,} bytes and {query.READ_CELLS:,} values" in text
    assert str(MARK)[:6] not in text and str(MARK)[:6] not in repr(error)
    # It was asked for alone, and read through to measure it.
    assert _asked(google)[-1] == 1


def test_a_row_past_the_drain_limit_says_over(small_caps, monkeypatch):
    monkeypatch.setattr(client_module, "DRAIN_BYTES", 20_000)
    google = _table_google([5_000])
    with pytest.raises(RowTooLarge) as raised:
        _pull(google, {"table": "sales.o"})
    assert not raised.value.complete
    assert "alone is over " in str(raised.value)
    assert "with over " in str(raised.value)


def test_a_row_over_the_real_cells_cap_is_refused():
    """At the real caps: ARRAY_AGG of 120,000 order ids in one row."""
    google = _query_google([120_000, 1])
    with pytest.raises(RowTooLarge) as raised:
        _pull(google, {"sql": SQL})
    assert raised.value.cells == 120_002 > query.READ_CELLS
    assert raised.value.row == 0
    assert "ARRAY_AGG(... LIMIT n)" in str(raised.value)


def test_a_row_over_the_real_bytes_cap_is_refused():
    google = FakeGoogle()
    big = query.READ_BYTES + 1_000
    google.add_table(
        f"{PROJECT}.sales.o",
        FakeTable([field_("s", "STRING")], [["x"], ["y" * big], ["z"]]),
    )
    with pytest.raises(RowTooLarge) as raised:
        _pull(google, {"table": "sales.o"})
    assert raised.value.row == 1 and raised.value.page_bytes > big
    assert "selected_fields" in str(raised.value)
    assert "yyyy" not in str(raised.value)


def test_the_tools_answer_a_row_over_the_cap_as_invalid_arguments(small_caps):
    toolkit = BigQueryToolkit(session=_query_google([2_500]))
    result = toolkit.call_tool("BIGQUERY_RUN_SELECT", {"sql": SQL}, blob())
    assert not result.successful and result.error_code == "invalid_arguments"
    assert "too large to read" in (result.error or "")
    toolkit = BigQueryToolkit(session=_table_google([2_500]))
    result = toolkit.call_tool("BIGQUERY_READ_TABLE_ROWS", {"table": "sales.o"}, blob())
    assert not result.successful and result.error_code == "invalid_arguments"


# -- the job of a dropped first page --------------------------------------------------


class _Reordered(FakeGoogle):
    """jobs.query answers its rows BEFORE its jobReference (or none)."""

    def __init__(self, *, job: bool) -> None:
        super().__init__()
        self.job = job

    def _answer(self, project, job_id, params):
        answer = super()._answer(project, job_id, params)
        body = json.loads(answer.content)
        reference = body.pop("jobReference")
        rows = body.pop("rows", None)
        out = {"rows": rows} if rows is not None else {}
        out.update(body)
        if self.job:
            out["jobReference"] = reference
        return FakeResponse(200, out)


def test_a_dropped_first_page_s_job_is_found_after_its_rows(small_caps):
    google = _Reordered(job=True)
    google.add_query(SQL, CannedQuery(FIELDS, _rows([300] * 40)))
    assert _ids(_pull(google, {"sql": SQL})) == list(range(40))
    assert _row_pages(google)[1]["url"].endswith("/queries/job_1")


def test_a_dropped_first_page_that_names_no_job_is_a_502(small_caps):
    google = _Reordered(job=False)
    google.add_query(SQL, CannedQuery(FIELDS, _rows([300] * 40)))
    with pytest.raises(PipelineError) as raised:
        _pull(google, {"sql": SQL})
    assert type(raised.value) is PipelineError
    assert host_answer(raised.value).status == 502
    assert "named no job" in str(raised.value)


# -- over a real requests.Session (urllib3 streams the body) -------------------------


def test_the_capped_read_streams_through_a_real_session(small_caps):
    google = _query_google([0] * 100 + [300] * 30)
    envelopes = _pull(real_session(google), {"sql": SQL})
    assert _ids(envelopes) == list(range(130))
    assert len(google.runs()) == 1


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (ConnectionResetError("reset"), "could not read BigQuery's answer"),
        (TimeoutError("timed out"), "did not answer in time"),
    ],
)
def test_a_row_page_that_breaks_off_midway_is_a_502(small_caps, error, says):
    google = _table_google([300] * 40)
    session = real_session(
        google,
        body_break=error,
        break_url=lambda url: url.endswith("/data"),
        break_after=5_000,
    )
    with pytest.raises(PipelineError) as raised:
        _pull(session, {"table": "sales.o"})
    assert type(raised.value) is PipelineError
    assert host_answer(raised.value).status == 502
    assert says in str(raised.value)
    assert "reset" not in str(raised.value)


# -- one wide row does not shrink the rest of the pull (#46 review seq 14088 M1) -----


#: Row-page requests a pull of one wide row then narrow rows may take: the
#: wide row's page, then pages that double back up to PAGE_ROWS, and the
#: rest (9 sized from the last page's mean cells a row; 15 from its widest
#: row, which asked for one row after the wide one: 1, 2, 4, ... 4,096).
WIDE_ROW_BUDGET = 20


@pytest.mark.parametrize("path", ["sql", "table"])
def test_one_wide_row_then_narrow_rows_all_come_back_in_a_few_requests(path):
    """At the real caps: row 0 holds 60,000 elements (more than PAGE_CELLS,
    under READ_CELLS), then 20,000 rows of one. Pages are sized from the LAST
    page's mean cells a row, never the pull's widest, and grow back by
    PAGE_GROWTH: the default max_rows (10,000) all come back, in order, cut
    by max_rows, within WIDE_ROW_BUDGET row pages."""
    lengths = [60_000] + [1] * 20_000
    if path == "sql":
        google = _query_google(lengths)
        envelopes = _pull(google, {"sql": SQL})
    else:
        google = _table_google(lengths)
        envelopes = _pull(google, {"table": "sales.o"})
    assert _ids(envelopes) == list(range(10_000))
    assert {e.metadata["truncated"] for e in envelopes} == {"max_rows"}
    if path == "table":
        assert envelopes[-1].metadata["next_start_index"] == 10_000
    asked = _asked(google)
    assert len(asked) <= WIDE_ROW_BUDGET, asked
    # It grew back: the last pages are far past one row.
    assert max(asked[-3:]) >= 1_000


def test_a_wide_row_midway_costs_a_few_requests_not_the_rest_of_the_pull():
    """max_rows 100,000; a 30,000-element row at row 5,000 of 100,000 rows of
    one element: every row comes back."""
    lengths = [1] * 100_000
    lengths[5_000] = 30_000
    google = _query_google(lengths)
    envelopes = _pull(google, {"sql": SQL, "max_rows": 100_000})
    ids = _ids(envelopes)
    assert ids == list(range(100_000))
    assert "truncated" not in envelopes[-1].metadata
    assert len(_asked(google)) <= 2 * WIDE_ROW_BUDGET, _asked(google)


#: Row-page requests 100,000 rows with a wide one every 500th may take: 104
#: sized from the last page's mean cells a row; 1,786 from its widest row
#: (#46 review seq 14138 nit a measured 1,793 with nine-digit elements).
RECURRING_WIDE_BUDGET = 150


@pytest.mark.parametrize("path", ["sql", "table"])
def test_a_wide_row_that_recurs_does_not_hold_each_next_page_to_one_row(path):
    """#46 review seq 14138 nit a, at the real caps: max_rows 100,000, and
    every 500th row holds 25,001 cells (24,999 one-digit elements, its
    column's cell and customer_id's), more than half of PAGE_CELLS; the rest
    hold 3. Sized from the last page's WIDEST row, the page after each wide
    one asked for one row and grew back a doubling at a time, only to meet
    the next: 1,793 requests, minutes of sequential getQueryResults calls.
    Sized from its MEAN cells a row, a page stays hundreds of rows. A page
    that meets two wide rows still fits READ_CELLS, and one over it is
    dropped and asked for again as before (the read cap bounds memory, not
    the sizing). Every row comes back, in order."""
    wide = [7] * 24_999  # one list, shared: the fake holds 100,000 rows
    rows = [[wide if i % 500 == 499 else [7], i] for i in range(100_000)]
    google = FakeGoogle()
    if path == "sql":
        google.add_dataset(f"{PROJECT}.sales")
        google.add_query(SQL, CannedQuery(FIELDS, rows))
        envelopes = _pull(google, {"sql": SQL, "max_rows": 100_000})
    else:
        google.add_table(f"{PROJECT}.sales.o", FakeTable(FIELDS, rows))
        envelopes = _pull(google, {"table": "sales.o", "max_rows": 100_000})
    assert _ids(envelopes) == list(range(100_000))
    assert "truncated" not in envelopes[-1].metadata
    asked = _asked(google)
    assert len(asked) <= RECURRING_WIDE_BUDGET, (len(asked), asked[:40])
    # Far past one row a page after the first wide row (widest: 1, 2, 4, ...).
    assert sorted(asked)[len(asked) // 2] >= 500


@pytest.mark.parametrize("path", ["sql", "table"])
def test_a_pull_stopped_by_max_pages_says_max_pages(path, monkeypatch):
    """MAX_PAGES pages read, rows left: truncated is max_pages (not
    max_rows), no page past MAX_PAGES is asked for, and a table pull's
    next_start_index continues after the last row read."""
    monkeypatch.setattr(query, "MAX_PAGES", 3)
    monkeypatch.setattr(query, "PAGE_ROWS", 10)
    lengths = [1] * 100
    if path == "sql":
        google = _query_google(lengths)
        envelopes = _pull(google, {"sql": SQL})
    else:
        google = _table_google(lengths)
        envelopes = _pull(google, {"table": "sales.o"})
    assert _ids(envelopes) == list(range(30))
    assert {e.metadata["truncated"] for e in envelopes} == {"max_pages"}
    assert len(_asked(google)) == 3
    if path == "table":
        assert envelopes[-1].metadata["next_start_index"] == 30


def test_page_rows_sizes_from_the_last_page_and_grows_back():
    fields = [field_("orders", "INT64", "REPEATED")]
    # The last page's rows held 60,000 cells each (its mean): one row.
    assert (
        query.page_rows(fields, 10_000, last_cells=60_000, last_rows=1, last_bytes=9)
        == 1
    )
    # Then a page of one 1-cell row: twice that (PAGE_GROWTH), not one again.
    assert query.page_rows(fields, 10_000, last_cells=1, last_rows=1, last_bytes=9) == 2
    assert query.page_rows(
        fields, 10_000, last_cells=1, last_rows=4_096, last_bytes=9
    ) == (8_192)
