"""Version markers on the pull envelope, so a scheduled re-check can tell an
edited file from an unchanged one (GoodRx item 4).

Dropbox gives every file a ``rev`` (a new one on each upload) and a
``content_hash`` (of the bytes alone). ``dropbox-source`` hands both on
verbatim, with ``server_modified``.

``FakeDropbox`` is the HTTP session the real Dropbox SDK posts through: it
answers ``files/list_folder`` and ``files/download`` in Dropbox's wire
format, so the real SDK parses them and the real ``DropboxClient`` runs.
Its content hash is Dropbox's algorithm (SHA-256 over the SHA-256 of each
4 MiB block). Any socket, connect or DNS lookup fails the test.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dropbox
import pytest
import requests
from aisquare.pipe.core.envelope import DataEnvelope, PullParams
from aisquare.pipe.testing.validation import no_network
from requests.structures import CaseInsensitiveDict

from aisquare_pipe_dropbox.connector import DropboxSource

API = "https://api.dropboxapi.com/2/"
CONTENT = "https://content.dropboxapi.com/2/"
BLOCK = 4 * 1024 * 1024


def content_hash(data: bytes) -> str:
    """Dropbox's content hash."""
    blocks = (data[i : i + BLOCK] for i in range(0, len(data), BLOCK))
    return hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in blocks)).hexdigest()


def _response(body: Any = None, *, content: bytes | None = None, headers: dict[str, str] | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = 200
    if content is None:
        content = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    r._content = content
    r.raw = io.BytesIO(content)
    r.headers = CaseInsensitiveDict(headers or {})
    r.encoding = "utf-8"
    return r


@dataclass
class _File:
    path: str
    content: bytes
    file_id: str
    rev: int
    hashed: bool = True


class FakeDropbox(requests.Session):
    """One Dropbox folder behind the API's RPC and download routes."""

    def __init__(self) -> None:
        super().__init__()
        self.files: dict[str, _File] = {}
        self.calls: list[str] = []
        self.after_listing: Callable[[], None] | None = None
        self._revs = 0

    def _next_rev(self) -> int:
        self._revs += 1
        return self._revs

    def add(self, path: str, content: bytes, *, hashed: bool = True) -> None:
        self.files[path] = _File(path, content, f"id:a4ayc_80_OEAAAAAAAAA{len(self.files) + 1}", self._next_rev(), hashed)

    def edit(self, path: str, content: bytes) -> None:
        self.files[path].content = content
        self.files[path].rev = self._next_rev()

    def entry(self, path: str) -> dict[str, Any]:
        f = self.files[path]
        when = f"2026-11-17T10:{f.rev:02d}:00Z"
        body: dict[str, Any] = {
            ".tag": "file",
            "name": f.path.rsplit("/", 1)[-1],
            "id": f.file_id,
            "path_lower": f.path.lower(),
            "path_display": f.path,
            "rev": f"015f{f.rev:08x}",
            "size": len(f.content),
            "server_modified": when,
            "client_modified": when,
            "is_downloadable": True,
        }
        if f.hashed:
            body["content_hash"] = content_hash(f.content)
        return body

    def post(self, url: str, data: Any = None, headers: Any = None, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        route = url.removeprefix(API) if url.startswith(API) else url.removeprefix(CONTENT)
        self.calls.append(route)
        if url == API + "files/list_folder":
            body = {"entries": [self.entry(p) for p in self.files], "cursor": "AAH4f99T0", "has_more": False}
            if self.after_listing is not None:
                self.after_listing()
            return _response(body)
        if url == CONTENT + "files/download":
            path = json.loads(headers["Dropbox-API-Arg"])["path"]
            (f,) = (f for f in self.files.values() if f.path.lower() == path)
            return _response(
                content=f.content,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Dropbox-API-Result": json.dumps(self.entry(f.path)),
                },
            )
        raise AssertionError(f"unexpected Dropbox call: POST {url}")


@pytest.fixture(autouse=True)
def _hermetic():
    with no_network() as log:
        yield
    assert not log.attempts, f"test touched the network: {log.attempts}"


@pytest.fixture
def fake(monkeypatch) -> FakeDropbox:
    session = FakeDropbox()
    real = dropbox.Dropbox
    monkeypatch.setattr(
        "aisquare_pipe_dropbox.client.dropbox.Dropbox",
        lambda **kw: real(session=session, **kw),
    )
    return session


def pull(**params: Any) -> dict[str, DataEnvelope]:
    """One pull of the root folder, by file name."""
    envelopes = DropboxSource().pull({"access_token": "sl.test-token"}, PullParams(params=params))
    return {e.metadata["filename"]: e for e in envelopes}


def markers(envelope: DataEnvelope) -> tuple[str, str, str]:
    m = envelope.metadata
    return m["rev"], m["content_hash"], m["server_modified"]


def test_content_hash_is_declared_beside_rev_in_the_metadata_spec():
    spec = DropboxSource.metadata_spec
    assert {"rev", "content_hash", "server_modified"} <= set(spec)
    assert (spec["content_hash"].type, spec["content_hash"].required) == (str, False)


def test_a_pulled_file_carries_its_rev_and_content_hash(fake):
    fake.add("/Policy.pdf", b"%PDF-1 policy")
    fake.add("/draft.pdf", b"%PDF-1 draft", hashed=False)

    pulled = pull()

    entry = fake.entry("/Policy.pdf")
    assert markers(pulled["Policy.pdf"]) == (entry["rev"], content_hash(b"%PDF-1 policy"), "2026-11-17T10:01:00")
    # Dropbox sent no hash: the key is absent, not "" or None.
    assert "content_hash" not in pulled["draft.pdf"].metadata
    assert pulled["draft.pdf"].metadata["rev"] == fake.entry("/draft.pdf")["rev"]


def test_a_streamed_file_carries_them_too(fake):
    fake.add("/Policy.pdf", b"%PDF-1 " + b"x" * 64)

    (envelope,) = pull(stream_threshold=16).values()

    assert envelope.stream is not None
    assert envelope.metadata["content_hash"] == fake.entry("/Policy.pdf")["content_hash"]


def test_the_markers_are_stable_across_pulls_of_unchanged_content(fake):
    fake.add("/Policy.pdf", b"%PDF-1 policy")

    first, second = pull()["Policy.pdf"], pull()["Policy.pdf"]

    assert markers(first) == markers(second)


def test_a_content_edit_moves_the_rev_and_the_content_hash(fake):
    fake.add("/Policy.pdf", b"%PDF-1 policy")
    before = pull()["Policy.pdf"]

    fake.edit("/Policy.pdf", b"%PDF-1 policy, revised")
    after = pull()["Policy.pdf"]

    assert after.metadata["rev"] != before.metadata["rev"]
    assert after.metadata["content_hash"] == content_hash(b"%PDF-1 policy, revised") != before.metadata["content_hash"]
    assert after.data == b"%PDF-1 policy, revised"


def test_the_markers_are_the_downloaded_bytes_not_the_listings(fake):
    """An edit after the folder listing: the download's own metadata (the
    same response as the bytes) gives the markers, so they match the bytes
    delivered."""
    fake.add("/Policy.pdf", b"%PDF-1 policy")
    listed = fake.entry("/Policy.pdf")
    fake.after_listing = lambda: fake.edit("/Policy.pdf", b"%PDF-1 policy, revised")

    envelope = pull()["Policy.pdf"]

    assert envelope.data == b"%PDF-1 policy, revised"
    current = fake.entry("/Policy.pdf")
    assert envelope.metadata["rev"] == current["rev"] != listed["rev"]
    assert envelope.metadata["content_hash"] == content_hash(envelope.data)
    assert envelope.metadata["server_modified"] == current["server_modified"].removesuffix("Z")
    assert fake.calls == ["files/list_folder", "files/download"]
