"""DatabricksFilesSource against a fake workspace: paging, recursion, markers, refusals."""

from __future__ import annotations

from typing import Any

import pytest
import requests
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_databricks import DatabricksFilesSource, DatabricksNotConfigured
from aisquare_pipe_databricks import connector as connector_mod
from aisquare_pipe_databricks.toolkit import DatabricksRequestError
from tests.fakes import FakeDatabricks, StoredFile, m2m_blob, pat_blob

VOL = "/Volumes/main/policies/docs"
_DEFAULT = object()


def _fake() -> FakeDatabricks:
    fake = FakeDatabricks()
    fake.volume[f"{VOL}/a.txt"] = StoredFile(
        b"a text", "text/plain", modified_ms=1_763_000_000_000
    )
    fake.volume[f"{VOL}/b.pdf"] = StoredFile(
        b"%PDF b", "application/pdf", modified_ms=1_763_400_000_000
    )
    fake.volume[f"{VOL}/c.md"] = StoredFile(
        b"# c", "text/markdown", modified_ms=1_763_500_000_000
    )
    fake.volume[f"{VOL}/big.bin"] = StoredFile(b"x" * 64, modified_ms=1_763_500_000_000)
    fake.volume[f"{VOL}/2026/d.txt"] = StoredFile(
        b"nested", "text/plain", modified_ms=1_763_600_000_000
    )
    fake.dbfs["/mnt/policies/e.txt"] = StoredFile(b"dbfs e", "text/plain")
    return fake


def _pull(
    fake: FakeDatabricks, params: dict | None = None, config: Any = _DEFAULT
) -> list:
    source = DatabricksFilesSource(session=fake)
    return list(
        source.pull(
            pat_blob() if config is _DEFAULT else config,
            PullParams(params=params or {}),
        )
    )


def test_pull_yields_each_file_with_its_version_markers_across_pages(monkeypatch):
    monkeypatch.setattr(connector_mod, "LIST_PAGE_SIZE", 2)
    fake = _fake()
    envelopes = _pull(fake, {"path": VOL})
    by_path = {e.metadata["path"]: e for e in envelopes}
    assert sorted(by_path) == [
        f"{VOL}/a.txt",
        f"{VOL}/b.pdf",
        f"{VOL}/big.bin",
        f"{VOL}/c.md",
    ]
    a = by_path[f"{VOL}/a.txt"]
    assert (a.data, a.content_type) == (b"a text", "text/plain")
    assert a.metadata["storage"] == "volume" and a.metadata["filename"] == "a.txt"
    assert (
        a.metadata["modified_time"] == "2025-11-17T10:00:00Z"
    )  # the Files API's Last-Modified
    assert a.metadata["etag"] == '"etag-1"' and len(a.metadata["sha256"]) == 64
    lists = [r for r in fake.api_calls() if "/fs/directories" in r["url"]]
    assert len(lists) == 3  # 5 entries (one a directory), 2 per page
    for envelope in envelopes:
        assert set(envelope.metadata) <= set(DatabricksFilesSource.metadata_spec)


def test_pull_recursive_dbfs_modified_after_and_limits():
    fake = _fake()
    nested = _pull(fake, {"path": f"{VOL}/", "recursive": True})
    assert f"{VOL}/2026/d.txt" in [e.metadata["path"] for e in nested]
    recent = _pull(fake, {"path": VOL, "modified_after": "2025-11-18T00:00:00Z"})
    assert sorted(e.metadata["path"] for e in recent) == [
        f"{VOL}/big.bin",
        f"{VOL}/c.md",
    ]
    small = _pull(fake, {"path": VOL, "max_bytes": 16})
    assert f"{VOL}/big.bin" not in [e.metadata["path"] for e in small]
    assert len(_pull(fake, {"path": VOL, "max_files": 1})) == 1
    dbfs = _pull(fake, {"path": "dbfs:/mnt/policies"}, m2m_blob())
    assert [(e.metadata["path"], e.data) for e in dbfs] == [
        ("dbfs:/mnt/policies/e.txt", b"dbfs e")
    ]
    single = _pull(fake, {"path": f"{VOL}/c.md"})
    assert [e.metadata["path"] for e in single] == [f"{VOL}/c.md"]


@pytest.mark.parametrize(
    "params",
    [
        {"path": VOL, "workspace_url": "https://evil.example"},
        {"path": VOL, "token": "dapi-mine"},
        {"path": VOL, "warehouse_id": "x"},
        {"path": "/Workspace/Users/someone"},
        {"path": "dbfs:/mnt/../etc"},
        {},
        {"path": VOL, "max_files": 0},
        {"path": VOL, "recursive": "yes"},
        {"path": VOL, "modified_after": "last week"},
        {"path": VOL, "unknown": 1},
    ],
)
def test_params_cannot_override_server_keys_or_leave_volumes_and_dbfs(params):
    fake = _fake()
    with pytest.raises(ValueError):
        _pull(fake, params)
    assert fake.requests == []


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"workspace_url": "https://acme.cloud.databricks.com"},
        pat_blob(token=""),
    ],
)
def test_pull_without_credentials_fails_closed_before_any_request(config):
    fake = _fake()
    with pytest.raises(DatabricksNotConfigured):
        _pull(fake, {"path": VOL}, config)
    assert fake.requests == []
    assert not DatabricksFilesSource().validate_config(config)  # type: ignore[arg-type]


# -- how pipe-as-service answers a failed pull (#46, seq 13861) --------------------

LEAKY = "/Volumes/main/policies/docs/member-123-45-6789.pdf is private"


@pytest.mark.parametrize(
    ("status", "headers", "answer"),
    [
        (400, {}, 422),
        (404, {}, 422),
        (401, {}, 502),
        (403, {}, 502),
        (429, {"Retry-After": "7"}, 502),
        (500, {}, 502),
        (503, {"Retry-After": "3"}, 502),
        (307, {}, 502),
    ],
)
def test_a_databricks_failure_is_a_502_or_a_422_without_its_text(
    status, headers, answer
):
    fake = _fake()
    fake.fail_with = (status, {"error_code": "X", "message": LEAKY}, headers)
    with pytest.raises(Exception) as raised:
        _pull(fake, {"path": f"{VOL}/a.txt"})
    got = host_answer(raised.value)
    assert got.status == answer, (status, type(raised.value))
    if answer == 502:
        assert type(raised.value) is PipelineError  # the host maps it by exact type
    else:
        assert isinstance(raised.value, DatabricksRequestError)
    assert "123-45-6789" not in str(raised.value)
    assert got.retry_after == (float(headers["Retry-After"]) if headers else None)


def test_a_missing_path_is_a_422():
    with pytest.raises(DatabricksRequestError) as raised:
        _pull(_fake(), {"path": f"{VOL}/nope.txt"})
    assert host_answer(raised.value).status == 422
    assert "does not exist" in str(raised.value)


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ChunkedEncodingError("connection broken: 123-45-6789"),
        requests.exceptions.ConnectionError("read timed out"),
    ],
)
def test_a_download_that_breaks_off_is_a_502(error):
    fake = _fake()
    fake.body_breaks = error
    with pytest.raises(PipelineError) as raised:
        _pull(fake, {"path": f"{VOL}/a.txt"})
    assert type(raised.value) is PipelineError
    assert host_answer(raised.value).status == 502
    assert "123-45-6789" not in str(raised.value)


def test_a_token_endpoint_that_does_not_answer_is_a_502():
    class Unreachable(FakeDatabricks):
        def request(self, method, url, **kwargs):
            if url.endswith("/oidc/v1/token"):
                raise requests.ConnectTimeout("no route")
            return super().request(method, url, **kwargs)

    with pytest.raises(PipelineError) as raised:
        list(
            DatabricksFilesSource(session=Unreachable()).pull(
                m2m_blob(), PullParams(params={"path": VOL})
            )
        )
    assert host_answer(raised.value).status == 502


@pytest.mark.parametrize("params", [["path"], "path=/Volumes/x", 7])
def test_pull_params_that_are_not_a_mapping_are_a_422(params):
    with pytest.raises(ValueError) as raised:
        list(
            DatabricksFilesSource(session=_fake()).pull(
                pat_blob(),
                PullParams(params=params),  # type: ignore[arg-type]
            )
        )
    assert host_answer(raised.value).status == 422


def test_an_unusable_port_in_the_blob_is_not_configured():
    """seq 13947 NIT: urllib's own ValueError never escapes the source."""
    fake = _fake()
    for url in (
        "https://a.cloud.databricks.com:abc",
        "https://a.cloud.databricks.com:99999",
    ):
        with pytest.raises(DatabricksNotConfigured, match="workspace_url must be"):
            _pull(fake, {"path": VOL}, pat_blob(workspace_url=url))
    assert fake.requests == []
