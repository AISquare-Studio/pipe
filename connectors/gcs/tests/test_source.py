"""GcsSource against a fake Google: paging, version markers, refusals."""

from __future__ import annotations

from typing import Any

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_gcs import GcsNotConfigured, GcsSource
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
