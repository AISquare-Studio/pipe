"""Version markers on the pull envelope, so a scheduled re-check can tell an
edited file from an unchanged one (GoodRx item 4).

Graph gives every file two markers: ``cTag`` changes only when the content
does, ``eTag`` on any change (a rename or a move too). ``onedrive-source``
hands both on verbatim as ``ctag`` and ``etag``.

``FakeGraph`` answers the three Graph calls a pull makes (the folder
listing, the item, its content) from an in-memory drive whose markers move
as Graph's do. It sits where ``requests.request`` is, so the real
``OneDriveClient`` runs; the conftest's socket guard fails any test that
reaches the network.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from aisquare.pipe.core.envelope import DataEnvelope, PullParams

from aisquare_pipe_onedrive.connector import OneDriveSource

from .fakes import CONFIG, GRAPH, listing, response

_ITEM = re.compile(r"/me/drive/items/([^/]+)")
_CONTENT = re.compile(r"/me/drive/items/([^/]+)/content")


@dataclass
class _File:
    item_id: str
    name: str
    content: bytes
    guid: str
    version: int = 1  # eTag's counter: any change
    content_version: int = 1  # cTag's counter: the bytes only
    markers: bool = True


class FakeGraph:
    """One OneDrive folder behind Graph's REST shape."""

    def __init__(self) -> None:
        self.files: dict[str, _File] = {}
        self.calls: list[str] = []
        self.after_listing: Callable[[], None] | None = None

    def add(self, item_id: str, name: str, content: bytes, *, markers: bool = True) -> None:
        guid = f"{{{len(self.files) + 1:08X}-1D2C-4B3A-9F8E-7D6C5B4A3F2E}}"
        self.files[item_id] = _File(item_id, name, content, guid, markers=markers)

    def edit(self, item_id: str, content: bytes) -> None:
        f = self.files[item_id]
        f.content = content
        f.version += 1
        f.content_version += 1

    def rename(self, item_id: str, name: str) -> None:
        f = self.files[item_id]
        f.name = name
        f.version += 1

    def item(self, item_id: str) -> dict[str, Any]:
        f = self.files[item_id]
        body: dict[str, Any] = {
            "id": f.item_id,
            "name": f.name,
            "size": len(f.content),
            "lastModifiedDateTime": f"2026-11-17T10:{f.version:02d}:00Z",
            "webUrl": f"https://contoso-my.sharepoint.com/personal/ana/Documents/{f.name}",
            "file": {"mimeType": "application/pdf"},
            "parentReference": {"driveId": "b!drive123", "path": "/drive/root:"},
        }
        if f.markers:
            body["eTag"] = f'"{f.guid},{f.version}"'
            body["cTag"] = f'"c:{f.guid},{f.content_version}"'
        return body

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        assert method == "GET", (method, url)
        assert url.startswith(GRAPH), url
        path = url.removeprefix(GRAPH)
        self.calls.append(path)
        if path == "/me/drive/root/children":
            body = listing([self.item(i) for i in self.files])
            if self.after_listing is not None:
                self.after_listing()
            return response(200, body)
        if m := _CONTENT.fullmatch(path):
            return response(200, content=self.files[m.group(1)].content, content_type="application/pdf")
        if m := _ITEM.fullmatch(path):
            return response(200, self.item(m.group(1)))
        raise AssertionError(f"unexpected Graph call: {method} {url}")


@pytest.fixture
def graph(monkeypatch) -> FakeGraph:
    fake = FakeGraph()
    monkeypatch.setattr("aisquare_pipe_onedrive.client.requests.request", fake.request)
    return fake


def pull(**params: Any) -> dict[str, DataEnvelope]:
    """One pull of the root folder, by file name."""
    envelopes = OneDriveSource().pull(CONFIG, PullParams(params=params))
    return {e.metadata["filename"]: e for e in envelopes}


def markers(envelope: DataEnvelope) -> tuple[str, str]:
    return envelope.metadata["ctag"], envelope.metadata["etag"]


def test_ctag_and_etag_are_declared_in_the_metadata_spec():
    spec = OneDriveSource.metadata_spec
    assert {"ctag", "etag"} <= set(spec)
    assert (spec["ctag"].type, spec["ctag"].required) == (str, False)
    assert (spec["etag"].type, spec["etag"].required) == (str, False)


def test_a_pulled_file_carries_graphs_ctag_and_etag(graph):
    graph.add("01A", "policy.pdf", b"%PDF-1 policy")
    graph.add("01B", "draft.pdf", b"%PDF-1 draft", markers=False)

    pulled = pull()

    item = graph.item("01A")
    assert markers(pulled["policy.pdf"]) == (item["cTag"], item["eTag"])
    assert item["cTag"] == '"c:{00000001-1D2C-4B3A-9F8E-7D6C5B4A3F2E},1"'  # verbatim, quotes kept
    # Graph sent none: the keys are absent, not "" (an empty marker would
    # compare equal to the next empty one and hide an edit).
    assert not {"ctag", "etag"} & set(pulled["draft.pdf"].metadata)


def test_a_streamed_file_carries_them_too(graph):
    graph.add("01A", "policy.pdf", b"%PDF-1 " + b"x" * 64)

    (envelope,) = pull(stream_threshold=16).values()

    assert envelope.stream is not None
    item = graph.item("01A")
    assert markers(envelope) == (item["cTag"], item["eTag"])


def test_the_markers_are_stable_across_pulls_of_unchanged_content(graph):
    graph.add("01A", "policy.pdf", b"%PDF-1 policy")

    first, second = pull()["policy.pdf"], pull()["policy.pdf"]

    assert markers(first) == markers(second)
    assert first.data == second.data


def test_a_content_edit_moves_the_ctag(graph):
    graph.add("01A", "policy.pdf", b"%PDF-1 policy")
    before = pull()["policy.pdf"]

    graph.edit("01A", b"%PDF-1 policy, revised")
    after = pull()["policy.pdf"]

    assert after.metadata["ctag"] != before.metadata["ctag"]
    assert after.metadata["etag"] != before.metadata["etag"]
    assert after.data == b"%PDF-1 policy, revised"


def test_a_rename_moves_the_etag_but_not_the_ctag(graph):
    graph.add("01A", "policy.pdf", b"%PDF-1 policy")
    before = pull()["policy.pdf"]

    graph.rename("01A", "policy-2026.pdf")
    after = pull()["policy-2026.pdf"]

    assert after.metadata["ctag"] == before.metadata["ctag"]  # same bytes: no re-read needed
    assert after.metadata["etag"] != before.metadata["etag"]


def test_the_markers_are_read_with_the_bytes_not_from_the_listing(graph):
    """An edit after the folder listing: the envelope carries the bytes the
    download got and the markers Graph gave for the item just before it,
    not the listing's older ones (which would make the next re-check read
    the file again for nothing)."""
    graph.add("01A", "policy.pdf", b"%PDF-1 policy")
    listed = graph.item("01A")
    graph.after_listing = lambda: graph.edit("01A", b"%PDF-1 policy, revised")

    envelope = pull()["policy.pdf"]

    assert envelope.data == b"%PDF-1 policy, revised"
    current = graph.item("01A")
    assert markers(envelope) == (current["cTag"], current["eTag"])
    assert envelope.metadata["ctag"] != listed["cTag"]
    assert graph.calls == ["/me/drive/root/children", "/me/drive/items/01A", "/me/drive/items/01A/content"]
