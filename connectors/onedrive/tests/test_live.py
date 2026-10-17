"""Live read-only checks against Microsoft Graph (opt-in).

Run with ``pipe validate onedrive --live`` or ``pytest -m live tests/test_live.py``.
Needs ``ONEDRIVE_ACCESS_TOKEN`` — a delegated Graph token with
``Files.Read`` (or ``Files.ReadWrite.All``) and ``User.Read``, e.g. from Graph
Explorer; self-skips without it. Optional ``ONEDRIVE_LIVE_QUERY`` (default
``a``) is the search keyword. No write tool is ever called.
"""

from __future__ import annotations

import os

import pytest

from aisquare_pipe_onedrive import OneDriveToolkit

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ONEDRIVE_ACCESS_TOKEN"), reason="ONEDRIVE_ACCESS_TOKEN not set"),
]

QUERY = os.environ.get("ONEDRIVE_LIVE_QUERY", "a")


@pytest.fixture(scope="module")
def call():
    toolkit = OneDriveToolkit()
    config = {"access_token": os.environ.get("ONEDRIVE_ACCESS_TOKEN", ""), "timeout_seconds": 60}

    def run(slug, arguments):
        result = toolkit.call_tool(slug, arguments, config)
        assert result.successful, f"{slug}: {result.error}"
        return result

    return run


@pytest.fixture(scope="module")
def root_children(call):
    return call("ONE_DRIVE_LIST_FOLDER_CHILDREN", {"top": 25}).data["items"]


def test_get_drive(call):
    data = call("ONE_DRIVE_GET_DRIVE", {}).data
    assert data["id"] and "quota" in data


def test_list_root(root_children):
    assert isinstance(root_children, list)
    assert all("@microsoft.graph.downloadUrl" not in item for item in root_children)


def test_list_root_by_path(call):
    data = call("ONE_DRIVE_LIST_FOLDER_CHILDREN", {"folder_path": "/", "top": 2}).data
    assert list(data)[:2] == ["has_more", "next_page_token"]


def test_search(call):
    data = call("ONE_DRIVE_SEARCH_ITEMS", {"q": QUERY, "top": 5}).data
    assert isinstance(data["items"], list)


def test_search_pagination(call):
    first = call("ONE_DRIVE_SEARCH_ITEMS", {"q": QUERY, "top": 1}).data
    if not first["next_page_token"]:
        pytest.skip("search returned a single page")
    second = call("ONE_DRIVE_SEARCH_ITEMS", {"q": QUERY, "top": 1, "page_token": first["next_page_token"]}).data
    assert isinstance(second["items"], list)


def test_get_item_and_permissions(call, root_children):
    if not root_children:
        pytest.skip("the drive root is empty")
    item = root_children[0]
    assert call("ONE_DRIVE_GET_ITEM", {"item_id": item["id"]}).data["id"] == item["id"]
    assert call("ONE_DRIVE_GET_ITEM_BY_PATH", {"item_path": item["name"]}).data["id"] == item["id"]
    assert isinstance(call("ONE_DRIVE_GET_ITEM_PERMISSIONS", {"item_id": item["id"]}).data["items"], list)


def test_download_a_small_file(call, root_children):
    files = [i for i in root_children if "file" in i and i.get("size", 0) <= 5 * 1024 * 1024]
    if not files:
        pytest.skip("no small file in the drive root")
    result = call("ONE_DRIVE_DOWNLOAD_FILE", {"item_id": files[0]["id"], "file_name": files[0]["name"]})
    assert len(result.files[0].data) == files[0]["size"]


@pytest.mark.parametrize("slug", ["ONE_DRIVE_GET_RECENT_ITEMS", "ONE_DRIVE_GET_SHARED_ITEMS"])
def test_retiring_endpoints_still_answer(call, slug):
    assert isinstance(call(slug, {}).data["items"], list)
