"""Live checks against the real Drive API (opt-in).

Run with ``pipe validate googledrive --live`` or ``pytest -m live tests/test_live.py``.
Needs ``GOOGLEDRIVE_ACCESS_TOKEN`` (an OAuth access token with the ``drive``
scope; e.g. from the OAuth Playground); self-skips without it. Read-only by
default. ``GOOGLEDRIVE_LIVE_WRITE=1`` also runs a write round trip that
creates a scratch folder and file and then moves both to the trash.
"""

from __future__ import annotations

import os
import uuid

import pytest

from aisquare_pipe_googledrive import (
    GoogleDriveSink,
    GoogleDriveSource,
    GoogleDriveToolkit,
)
from aisquare_pipe_googledrive.formats import is_native

TOKEN = os.environ.get("GOOGLEDRIVE_ACCESS_TOKEN", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not TOKEN, reason="GOOGLEDRIVE_ACCESS_TOKEN not set"),
]

READS = [
    ("GOOGLEDRIVE_GET_ABOUT", {}),
    ("GOOGLEDRIVE_FIND_FILE", {"q": "trashed = false", "pageSize": 3}),
    ("GOOGLEDRIVE_LIST_FILES", {"folderId": "root", "pageSize": 3}),
    ("GOOGLEDRIVE_FIND_FOLDER", {"page_size": 3}),
    ("GOOGLEDRIVE_LIST_SHARED_DRIVES", {"pageSize": 3}),
]


@pytest.fixture(scope="module")
def call():
    toolkit = GoogleDriveToolkit()
    config = {"access_token": TOKEN, "timeout_seconds": 60}

    def run(slug, arguments):
        result = toolkit.call_tool(slug, arguments, config)
        assert result.successful, f"{slug}: {result.error}"
        return result

    return run


@pytest.mark.parametrize(("slug", "arguments"), READS, ids=[s.removeprefix("GOOGLEDRIVE_") for s, _ in READS])
def test_read_tool(call, slug, arguments):
    data = call(slug, arguments).data
    assert isinstance(data, dict)
    if "items" in data:
        assert list(data)[:2] == ["has_more", "next_page_token"]


def test_file_reads(call):
    items = call("GOOGLEDRIVE_FIND_FILE", {"q": "trashed = false and mimeType != 'application/vnd.google-apps.folder'", "pageSize": 20}).data["items"]
    if not items:
        pytest.skip("the Drive has no files")
    small = next((f for f in items if is_native(f["mimeType"]) or int(f.get("size") or 0) < 1_000_000), None)
    if small is None:
        pytest.skip("no small file to download")
    meta = call("GOOGLEDRIVE_GET_FILE_METADATA", {"file_id": small["id"]}).data
    assert meta["id"] == small["id"]
    download = call("GOOGLEDRIVE_DOWNLOAD_FILE", {"file_id": small["id"]})
    assert download.files and download.data["size"] == len(download.files[0].data)
    call("GOOGLEDRIVE_LIST_PERMISSIONS", {"fileId": small["id"]})
    call("GOOGLEDRIVE_LIST_COMMENTS", {"fileId": small["id"], "pageSize": 5})


def test_bad_token_is_unauthorized():
    result = GoogleDriveToolkit().call_tool("GOOGLEDRIVE_GET_ABOUT", {}, {"access_token": "ya29.definitelyNotAValidToken"})
    assert result.successful is False
    assert result.error_code == "unauthorized"


def test_source_pulls_from_root():
    from aisquare.pipe import PullParams

    pulled = list(GoogleDriveSource().pull({"access_token": TOKEN}, PullParams({"limit": 2, "stream_threshold": 1_000_000})))
    for envelope in pulled:
        assert envelope.metadata["file_id"] and envelope.metadata["filename"]


@pytest.mark.skipif(os.environ.get("GOOGLEDRIVE_LIVE_WRITE") != "1", reason="GOOGLEDRIVE_LIVE_WRITE != 1")
def test_write_round_trip(call):
    from aisquare.pipe import DataEnvelope, PushParams

    folder = call("GOOGLEDRIVE_CREATE_FOLDER", {"name": f"aisquare-pipe-live-{uuid.uuid4().hex[:8]}"}).data
    try:
        created = call(
            "GOOGLEDRIVE_CREATE_FILE_FROM_TEXT",
            {"file_name": "hello.md", "text_content": "# hello", "mime_type": "text/markdown", "parent_id": folder["id"]},
        ).data
        assert call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": created["id"]}).data["content"] == "# hello"
        call("GOOGLEDRIVE_EDIT_FILE", {"file_id": created["id"], "content": "# bye", "mime_type": "text/markdown"})
        call("GOOGLEDRIVE_UPDATE_FILE_PUT", {"fileId": created["id"], "name": "bye.md"})
        pushed = GoogleDriveSink().push(
            DataEnvelope(content_type="text/plain", data=b"x" * 6_000_000, source_id="live", metadata={"filename": "big.txt"}),
            {"access_token": TOKEN},
            PushParams({"folder_id": folder["id"]}),
        )
        assert pushed.success, pushed.error
        assert pushed.metadata["size"] == 6_000_000
    finally:
        call("GOOGLEDRIVE_DELETE_FILE", {"fileId": folder["id"]})
