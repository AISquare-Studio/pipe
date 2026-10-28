"""Regression: ``OneDriveClient.list_folder(recursive=True)`` used to re-list
only the *first* page of a folder to find subfolders, so a subfolder on page
2+ was never descended into. Every page is now scanned for subfolders, and
each subfolder listing is paginated too."""

from __future__ import annotations

from unittest.mock import MagicMock

from aisquare_pipe_onedrive.client import OneDriveClient

from .helpers import make_file_item, make_folder_item, make_graph_response

ROOT = "https://graph.microsoft.com/v1.0/me/drive/root/children"


def _page(body):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = body
    return resp


def test_subfolder_on_a_later_page_is_descended(mock_requests, sample_config):
    pages = {
        ROOT: make_graph_response([make_file_item("a.txt")], next_link="https://next/root-2"),
        "https://next/root-2": make_graph_response([make_folder_item("Deep"), make_file_item("b.txt")]),
        "https://graph.microsoft.com/v1.0/me/drive/root:/Deep:/children": make_graph_response(
            [make_file_item("c.txt")], next_link="https://next/deep-2"
        ),
        "https://next/deep-2": make_graph_response([make_folder_item("Deeper"), make_file_item("d.txt")]),
        "https://graph.microsoft.com/v1.0/me/drive/root:/Deep/Deeper:/children": make_graph_response(
            [make_file_item("e.txt")]
        ),
    }
    mock_requests.side_effect = lambda method, url, **kw: _page(pages[url])

    names = [item["name"] for item in OneDriveClient(sample_config).list_folder("/", recursive=True)]

    assert names == ["a.txt", "Deep", "b.txt", "c.txt", "Deeper", "d.txt", "e.txt"]
    requested = [call.args[1] for call in mock_requests.call_args_list]
    assert requested == list(pages)  # each page fetched exactly once, no re-listing


def test_non_recursive_listing_ignores_subfolders(mock_requests, sample_config):
    mock_requests.return_value = _page(make_graph_response([make_folder_item("Sub"), make_file_item("a.txt")]))
    items = list(OneDriveClient(sample_config).list_folder(""))
    assert [i["name"] for i in items] == ["Sub", "a.txt"]
    assert mock_requests.call_count == 1
