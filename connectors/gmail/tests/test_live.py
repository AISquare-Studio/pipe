"""Live checks against the real Gmail API (opt-in).

Run with ``pipe validate gmail --live`` or ``pytest -m live tests/test_live.py``.
Needs ``GMAIL_ACCESS_TOKEN`` (an OAuth access token with the
``gmail.readonly`` scope; e.g. from the OAuth Playground); self-skips
without it. Read-only: nothing is sent, drafted, labelled or trashed.
"""

from __future__ import annotations

import os

import pytest

from aisquare_pipe_gmail import GmailToolkit

TOKEN = os.environ.get("GMAIL_ACCESS_TOKEN", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not TOKEN, reason="GMAIL_ACCESS_TOKEN not set"),
]

READS = [
    ("GMAIL_GET_PROFILE", {}),
    ("GMAIL_LIST_LABELS", {}),
    ("GMAIL_FETCH_EMAILS", {"max_results": 2}),
    ("GMAIL_LIST_THREADS", {"max_results": 2, "verbose": True}),
]


@pytest.fixture(scope="module")
def call():
    toolkit = GmailToolkit()
    config = {"access_token": TOKEN, "timeout_seconds": 60}

    def run(slug, arguments):
        result = toolkit.call_tool(slug, arguments, config)
        assert result.successful, f"{slug}: {result.error}"
        return result

    return run


@pytest.mark.parametrize(("slug", "arguments"), READS, ids=[s.removeprefix("GMAIL_") for s, _ in READS])
def test_read_tool(call, slug, arguments):
    data = call(slug, arguments).data
    assert isinstance(data, dict)
    if "items" in data:
        assert list(data)[:2] == ["has_more", "next_page_token"]


def test_message_thread_and_attachment(call):
    items = call("GMAIL_FETCH_EMAILS", {"query": "has:attachment larger:1 smaller:1M", "max_results": 1}).data["items"]
    if not items:
        pytest.skip("no message with a small attachment")
    found = items[0]
    full = call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": found["messageId"]}).data
    assert full["messageId"] == found["messageId"]
    call("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {"thread_id": found["threadId"]})
    attachment = next((a for a in full["attachmentList"] if a.get("attachmentId")), None)
    if attachment is None:
        pytest.skip("the attachment is inline only")
    got = call(
        "GMAIL_GET_ATTACHMENT",
        {"message_id": found["messageId"], "attachment_id": attachment["attachmentId"], "file_name": attachment["filename"]},
    )
    assert got.files and got.data["size"] == len(got.files[0].data)


def test_bad_token_is_unauthorized():
    result = GmailToolkit().call_tool("GMAIL_GET_PROFILE", {}, {"access_token": "ya29.definitelyNotAValidToken"})
    assert result.successful is False
    assert result.error_code == "unauthorized"
