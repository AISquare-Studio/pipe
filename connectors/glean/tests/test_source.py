"""GleanSource: documents as text envelopes with the update-time marker."""

from __future__ import annotations

import hashlib
import logging

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_glean import (
    GleanAPIError,
    GleanNotConfigured,
    GleanSource,
    connector,
)
from tests.fakes import (
    GLOBAL_BLOB,
    HOST,
    TOKEN,
    USER_BLOB,
    FakeSession,
    document,
    documents,
    response,
    search_page,
)


def _source(*responses):
    session = FakeSession().queue(*responses)
    return GleanSource(session=session), session


def _pull(source, params, config=USER_BLOB):
    return list(source.pull(config, PullParams(params)))


def test_pull_by_query_pages_dedupes_and_reads_each_document_once():
    src, session = _source(
        response(200, search_page("d1", "d2", cursor="c-2")),
        response(200, search_page("d2", "d3")),
        response(200, documents(document("d1"), document("d2"), document("d3"))),
    )
    envelopes = _pull(src, {"query": "policy", "datasources": ["confluence"]})
    assert [e.metadata["glean_document_id"] for e in envelopes] == ["d1", "d2", "d3"]
    assert [c.endpoint for c in session.calls] == ["search", "search", "getdocuments"]
    assert session.calls[1].json["cursor"] == "c-2"
    assert session.calls[0].json["requestOptions"]["datasourcesFilter"] == [
        "confluence"
    ]
    assert session.calls[2].json == {
        "documentSpecs": [{"id": "d1"}, {"id": "d2"}, {"id": "d3"}],
        "includeFields": ["DOCUMENT_CONTENT"],
    }


def test_envelope_carries_the_text_and_the_update_time_marker():
    src, _ = _source(
        response(200, documents(document("d1", updated="2026-11-15T08:30:00Z")))
    )
    (env,) = _pull(src, {"document_ids": ["d1"]})
    assert (env.content_type, env.source_id) == ("text/plain", "glean-source")
    assert env.data == "line one of d1\nline two"
    assert env.metadata == {
        "glean_document_id": "d1",
        "title": "Doc d1",
        "url": "https://docs.acme.com/d1",
        "datasource": "confluence",
        "mime_type": "text/html",
        "update_time": "2026-11-15T08:30:00Z",
        "create_time": "2026-01-01T00:00:00Z",
        "author": "Ann Author",
    }


def test_an_edited_document_shows_a_new_marker():
    src, _ = _source(
        response(200, documents(document("d1", updated="2026-11-01T00:00:00Z"))),
        response(
            200,
            documents(document("d1", text=["edited"], updated="2026-11-02T00:00:00Z")),
        ),
    )
    before = _pull(src, {"document_ids": ["d1"]})[0]
    after = _pull(src, {"document_ids": ["d1"]})[0]
    assert before.metadata["update_time"] != after.metadata["update_time"]
    assert after.data == "edited"


def test_a_document_without_update_time_falls_back_to_a_content_hash_marker(caplog):
    src, _ = _source(
        response(
            200,
            documents(
                document("d1", updated=None),
                document("d2"),
                document("d3", text=["other text"], updated=None),
            ),
        )
    )
    with caplog.at_level(logging.INFO, logger="aisquare.pipe.glean"):
        d1, d2, d3 = _pull(src, {"document_ids": ["d1", "d2", "d3"]})
    # No updateTime: the marker is the SHA-256 of the text, under its own key.
    for env in (d1, d3):
        assert "update_time" not in env.metadata
        assert (
            env.metadata["content_sha256"]
            == hashlib.sha256(env.data.encode("utf-8")).hexdigest()
        )
    assert d1.metadata["content_sha256"] != d3.metadata["content_sha256"]
    # With updateTime, that is the marker and there is no hash.
    assert d2.metadata["update_time"] == "2026-11-01T10:00:00Z"
    assert "content_sha256" not in d2.metadata
    # Counted in one line, with no ids or text.
    assert [r.getMessage() for r in caplog.records] == [
        (
            "glean-source: 2 document(s) had no updateTime; their version marker "
            "is content_sha256"
        )
    ]


def test_the_content_hash_marker_changes_when_the_text_changes():
    src, _ = _source(
        response(200, documents(document("d1", updated=None))),
        response(200, documents(document("d1", text=["edited"], updated=None))),
    )
    before = _pull(src, {"document_ids": ["d1"]})[0]
    after = _pull(src, {"document_ids": ["d1"]})[0]
    assert before.metadata["content_sha256"] != after.metadata["content_sha256"]


def test_hitting_the_search_page_cap_logs_one_warning_with_counts_only(
    caplog, monkeypatch
):
    monkeypatch.setattr(connector, "MAX_SEARCH_PAGES", 2)
    src, session = _source(
        response(200, search_page("d1", cursor="c-2")),
        response(200, search_page("d2", cursor="c-3")),  # Glean still has more
        response(200, documents(document("d1"), document("d2"))),
    )
    with caplog.at_level(logging.INFO, logger="aisquare.pipe.glean"):
        envelopes = _pull(src, {"query": "secret policy", "max_documents": 10})
    assert [e.metadata["glean_document_id"] for e in envelopes] == ["d1", "d2"]
    assert [c.endpoint for c in session.calls] == ["search", "search", "getdocuments"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        (
            "glean-source: stopped the search at the 2-page cap with more results "
            "available; read 2 of up to 10 document(s)"
        )
    ]
    assert "secret policy" not in caplog.text and "d1" not in caplog.text


@pytest.mark.parametrize(
    "pages",
    [
        # Glean says there is nothing more.
        [search_page("d1", cursor="c-2"), search_page("d2")],
        # max_documents is reached first.
        [search_page("d1", cursor="c-2"), search_page("d2", "d3", cursor="c-3")],
    ],
)
def test_a_search_that_ends_within_the_cap_logs_no_warning(caplog, monkeypatch, pages):
    monkeypatch.setattr(connector, "MAX_SEARCH_PAGES", 2)
    src, _ = _source(
        *(response(200, p) for p in pages),
        response(200, documents(document("d1"), document("d2"))),
    )
    with caplog.at_level(logging.INFO, logger="aisquare.pipe.glean"):
        _pull(src, {"query": "q", "max_documents": 2})
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_document_ids_are_read_in_batches_of_twenty():
    ids = [f"d{i:02d}" for i in range(45)]
    batches = [ids[0:20], ids[20:40], ids[40:45]]
    src, session = _source(
        *(response(200, documents(*(document(i) for i in b))) for b in batches)
    )
    envelopes = _pull(src, {"document_ids": ids + ids[:3]})  # duplicates read once
    assert [e.metadata["glean_document_id"] for e in envelopes] == ids
    assert [
        [s["id"] for s in c.json["documentSpecs"]] for c in session.calls
    ] == batches


def test_max_documents_bounds_the_search():
    src, session = _source(
        response(200, search_page("d1", "d2", "d3", cursor="more")),
        response(200, documents(document("d1"), document("d2"))),
    )
    envelopes = _pull(src, {"query": "q", "max_documents": 2})
    assert [e.metadata["glean_document_id"] for e in envelopes] == ["d1", "d2"]
    assert session.calls[0].json["pageSize"] == 2
    assert len(session.calls) == 2  # the cursor is not followed past the limit


def test_unreadable_documents_are_skipped_and_counted_without_ids(caplog):
    src, _ = _source(
        response(200, documents(document("d1"), errors={"d2": "permission denied"}))
    )
    with caplog.at_level(logging.INFO, logger="aisquare.pipe.glean"):
        envelopes = _pull(src, {"document_ids": ["d1", "d2"]})
    assert [e.metadata["glean_document_id"] for e in envelopes] == ["d1"]
    assert [r.getMessage() for r in caplog.records] == [
        "glean-source: skipped 1 document(s) Glean could not read"
    ]


def test_global_token_sends_act_as_on_every_pull_request():
    src, session = _source(
        response(200, search_page("d1")), response(200, documents(document("d1")))
    )
    _pull(src, {"query": "q"}, GLOBAL_BLOB)
    assert [c.headers.get("X-Glean-ActAs") for c in session.calls] == [
        "reader@acme.com"
    ] * 2


# --- fail closed -------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"host": HOST},
        {"token": TOKEN},
        {**USER_BLOB, "host": "evil.com"},
        {**GLOBAL_BLOB, "act_as": ""},
    ],
)
def test_pull_without_usable_credentials_fails_closed_before_any_request(config):
    src, session = _source()
    with pytest.raises(GleanNotConfigured) as e:
        _pull(src, {"query": "q"}, config)
    assert TOKEN not in str(e.value)
    assert session.calls == []
    assert src.validate_config(config) is False


@pytest.mark.parametrize(
    "key", ["host", "token", "token_kind", "act_as", "allowed_hosts"]
)
def test_pull_params_cannot_override_a_server_key(key):
    src, session = _source()
    with pytest.raises(ValueError, match="server-controlled"):
        _pull(src, {"query": "q", key: "evil.com"})
    assert session.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"query": "q", "document_ids": ["d1"]},
        {"query": " "},
        {"document_ids": "d1"},
        {"document_ids": ["d1"], "datasources": ["x"]},
        {"query": "q", "max_documents": 0},
        {"query": "q", "max_documents": 1001},
        {"query": "q", "base_url": "https://evil.com"},
    ],
)
def test_bad_pull_params_are_refused_without_a_request(params):
    src, session = _source()
    with pytest.raises(ValueError):
        _pull(src, params)
    assert session.calls == []


def test_glean_failure_surfaces_as_glean_api_error_without_the_token():
    src, _ = _source(response(401, {"message": f"bad token {TOKEN}"}))
    with pytest.raises(GleanAPIError) as e:
        _pull(src, {"query": "q"})
    assert e.value.status == 401
    assert TOKEN not in str(e.value)
