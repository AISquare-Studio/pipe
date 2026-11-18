"""GcsSource against a fake Google: paging, version markers, refusals."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_gcs import GcsNotConfigured, GcsPullTruncated, GcsSource
from aisquare_pipe_gcs import connector as connector_mod
from tests.fakes import FakeGoogle, StoredObject, blob


def _google() -> FakeGoogle:
    return FakeGoogle(
        objects={
            "policies/a.pdf": StoredObject(
                b"%PDF a",
                "application/pdf",
                generation="11",
                updated="2026-11-10T00:00:00Z",
            ),
            "policies/b.txt": StoredObject(
                b"b text", "text/plain", generation="12", updated="2026-11-15T00:00:00Z"
            ),
            "policies/c.md": StoredObject(
                b"# c", "text/markdown", generation="13", updated="2026-11-16T00:00:00Z"
            ),
            "policies/sub/": StoredObject(
                b"", "application/x-directory", generation="14"
            ),
            "policies/big.bin": StoredObject(
                b"x" * 64, "application/octet-stream", generation="15"
            ),
            "hr/secret.csv": StoredObject(b"no", "text/csv", generation="16"),
        }
    )


_DEFAULT = object()


def _pull(
    google_: FakeGoogle, params: dict | None = None, config: Any = _DEFAULT
) -> list:
    source = GcsSource(session=google_)
    return list(
        source.pull(
            blob() if config is _DEFAULT else config, PullParams(params=params or {})
        )
    )


def test_pull_yields_each_object_with_its_version_markers_across_pages(monkeypatch):
    monkeypatch.setattr(connector_mod, "LIST_PAGE_SIZE", 2)
    google_ = _google()
    envelopes = _pull(google_)
    by_name = {e.metadata["object_name"]: e for e in envelopes}
    assert sorted(by_name) == [
        "policies/a.pdf",
        "policies/b.txt",
        "policies/big.bin",
        "policies/c.md",
    ]
    b = by_name["policies/b.txt"]
    assert (b.data, b.content_type) == (b"b text", "text/plain")
    assert b.metadata["generation"] == "12"
    assert b.metadata["md5_hash"] == "XrY7u+Ae7tCTyyK7j1rNww=="
    assert b.metadata["updated"] == "2026-11-15T00:00:00Z"
    assert (b.metadata["bucket"], b.metadata["filename"], b.metadata["size"]) == (
        "acme-policies",
        "b.txt",
        6,
    )
    lists = [c for c in google_.storage_calls() if c["url"].endswith("/o")]
    assert len(lists) == 3  # 5 names under the prefix, 2 per page
    for envelope in envelopes:
        assert set(envelope.metadata) <= set(GcsSource.metadata_spec)


def test_pull_skips_folders_and_objects_over_max_bytes():
    envelopes = _pull(_google(), {"max_bytes": 32})
    names = [e.metadata["object_name"] for e in envelopes]
    assert "policies/sub/" not in names and "policies/big.bin" not in names
    assert len(names) == 3


def test_pull_only_objects_updated_after():
    envelopes = _pull(_google(), {"modified_after": "2026-11-14T00:00:00Z"})
    assert sorted(e.metadata["object_name"] for e in envelopes) == [
        "policies/b.txt",
        "policies/big.bin",
        "policies/c.md",
    ]


def test_pull_named_objects_and_a_narrower_prefix():
    google_ = _google()
    named = _pull(google_, {"object_names": ["policies/c.md", "policies/a.pdf"]})
    assert [e.metadata["object_name"] for e in named] == [
        "policies/c.md",
        "policies/a.pdf",
    ]
    narrower = _pull(google_, {"prefix": "policies/b."})
    assert [e.metadata["object_name"] for e in narrower] == ["policies/b.txt"]
    limited = _pull(google_, {"max_objects": 1})
    assert len(limited) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"bucket": "someone-elses-bucket"},
        {"service_account_json": {"type": "service_account"}},
        {"prefix": "hr/"},
        {"object_names": ["hr/secret.csv"]},
        {"object_names": ["policies/a.pdf"], "prefix": "policies/"},
        {"object_names": ["policies/a.pdf"], "start_offset": "policies/b"},
        {"start_offset": "hr/secret.csv"},
        {"prefix": "policies/b", "start_offset": "policies/a.pdf"},
        {"start_offset": ["policies/a.pdf"]},
        {"object_names": ["policies/a.pdf", "policies/b.txt"], "max_objects": 1},
        {"max_objects": 0},
        {"modified_after": "yesterday"},
        {"unknown": 1},
    ],
)
def test_params_cannot_override_server_keys_or_leave_the_prefix(params):
    google_ = _google()
    with pytest.raises(ValueError):
        _pull(google_, params)
    assert google_.requests == []


@pytest.mark.parametrize(
    "config", [None, {}, {"bucket": "acme-policies"}, blob(bucket="")]
)
def test_pull_without_credentials_fails_closed_before_any_request(config):
    google_ = _google()
    with pytest.raises(GcsNotConfigured):
        _pull(google_, config=config)
    assert google_.requests == []
    assert not GcsSource().validate_config(config)  # type: ignore[arg-type]


def _many(count: int) -> FakeGoogle:
    google_ = FakeGoogle(
        objects={
            f"policies/doc{i:04d}.txt": StoredObject(
                f"doc {i}".encode(),
                "text/plain",
                generation=str(100 + i),
                updated=f"2026-11-{1 + i % 28:02d}T00:00:00Z",
            )
            for i in range(count)
        }
    )
    google_.objects["policies/zz/"] = StoredObject(b"", "application/x-directory")
    return google_


@pytest.mark.parametrize("page_size", [2, 3, 1000])
def test_pull_stops_at_max_objects_says_so_and_resumes_with_start_offset(
    monkeypatch, caplog, page_size
):
    """1500 objects, 1000 per pull (the probe's case, scaled down: 7 and 3):
    the last envelope says where to go on, and start_offset gets the rest
    with nothing missed or repeated. Page sizes 2 and 3 cut at a page's end
    and mid-page."""
    monkeypatch.setattr(connector_mod, "LIST_PAGE_SIZE", page_size)
    google_ = _many(7)
    seen: list[str] = []
    params: dict[str, Any] = {"max_objects": 3}
    for rounds in range(1, 11):  # bounded: a bug can't loop forever
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="aisquare.pipe.gcs"):
            envelopes = _pull(google_, params)
        seen += [e.metadata["object_name"] for e in envelopes]
        for envelope in envelopes[:-1]:
            assert "truncated" not in envelope.metadata
        last = envelopes[-1].metadata
        if "truncated" not in last:
            assert not caplog.records
            break
        assert last["truncated"] == "max_objects"
        assert last["next_start_offset"] > last["object_name"]
        (warning,) = caplog.records
        assert "stopped early (max_objects)" in warning.getMessage()
        assert "policies/doc" not in warning.getMessage()  # no names in logs
        params = {"max_objects": 3, "start_offset": last["next_start_offset"]}
    else:
        pytest.fail("the pulls never reached the end of the listing")
    assert seen == [f"policies/doc{i:04d}.txt" for i in range(7)]
    assert rounds == 3
    lists = [c for c in google_.storage_calls() if c["url"].endswith("/o")]
    assert lists[-1]["params"]["startOffset"] == "policies/doc0006.txt"


def test_a_pull_that_ends_exactly_at_max_objects_is_not_truncated(monkeypatch):
    monkeypatch.setattr(connector_mod, "LIST_PAGE_SIZE", 2)
    google_ = _many(4)
    del google_.objects["policies/zz/"]
    envelopes = _pull(google_, {"max_objects": 4})
    assert len(envelopes) == 4 and "truncated" not in envelopes[-1].metadata


def test_a_page_capped_pull_with_no_envelope_raises_where_to_continue(
    monkeypatch, caplog
):
    """modified_after that matches little in a big prefix: the listing stops
    at MAX_LIST_PAGES with nothing to return, so no envelope can carry the
    marker. The pull raises GcsPullTruncated with next_start_offset instead
    (tester E1, review m3): before, it ended with 0 envelopes and no way on."""
    monkeypatch.setattr(connector_mod, "LIST_PAGE_SIZE", 2)
    monkeypatch.setattr(connector_mod, "MAX_LIST_PAGES", 2)
    google_ = _many(9)  # 9 documents and "policies/zz/": 4 names per pull
    with (
        caplog.at_level(logging.WARNING, logger="aisquare.pipe.gcs"),
        pytest.raises(GcsPullTruncated) as caught,
    ):
        _pull(google_, {"modified_after": "2027-01-01T00:00:00Z"})
    error = caught.value
    assert isinstance(error, ValueError)  # a host's client error (422), not a 500
    assert (error.reason, error.next_start_offset) == (
        "max_list_pages",
        "policies/doc0004.txt",
    )
    assert "start_offset='policies/doc0004.txt'" in str(error)
    (warning,) = caplog.records
    assert "stopped early (max_list_pages)" in warning.getMessage()
    assert "no envelope came out" in warning.getMessage()
    assert "policies/doc" not in warning.getMessage()  # no names in logs


def test_a_recurring_modified_after_sync_advances_to_the_end(monkeypatch):
    """The README's loop: follow next_start_offset, from the error or from
    the last envelope, until a pull is not truncated. Every name is listed
    once and the one change is found."""
    monkeypatch.setattr(connector_mod, "LIST_PAGE_SIZE", 2)
    monkeypatch.setattr(connector_mod, "MAX_LIST_PAGES", 2)
    google_ = _many(9)
    google_.objects["policies/doc0006.txt"].updated = "2027-02-01T00:00:00Z"
    since = {"modified_after": "2027-01-01T00:00:00Z"}
    params: dict[str, Any] = dict(since)
    found: list[str] = []
    offsets: list[str] = []
    for _ in range(10):  # bounded: a bug can't loop forever
        try:
            envelopes = _pull(google_, params)
        except GcsPullTruncated as e:
            assert e.next_start_offset is not None
            offsets.append(e.next_start_offset)
            params = {**since, "start_offset": e.next_start_offset}
            continue
        found += [e.metadata["object_name"] for e in envelopes]
        if not envelopes or "truncated" not in envelopes[-1].metadata:
            break
        offsets.append(envelopes[-1].metadata["next_start_offset"])
        params = {**since, "start_offset": offsets[-1]}
    else:
        pytest.fail("the sync never reached the end of the listing")
    assert found == ["policies/doc0006.txt"]
    assert offsets == ["policies/doc0004.txt", "policies/doc0008.txt"]
    pages = [c for c in google_.storage_calls() if c["params"].get("maxResults") == 2]
    assert len(pages) == 5  # 2 + 2 + 1 listing pages: none listed twice


@pytest.mark.parametrize("name", [".", ".."])
def test_pull_refuses_dot_names(name):
    google_ = _google()
    with pytest.raises(ValueError):
        _pull(google_, {"object_names": [name]}, config=blob(prefix=None))
    assert google_.requests == []
