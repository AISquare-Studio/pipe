"""Live checks against a real Slack workspace — opt-in only.

Run with ``pytest -m live`` (or ``pipe validate --live``). Skipped unless
``SLACK_TEST_USER_TOKEN`` and ``SLACK_TEST_CHANNEL`` (a dedicated test
channel's ID, on the staging app) are set; ``SLACK_TEST_BOT_TOKEN`` adds the
bot-identity checks. ``SLACK_TEST_DM`` (a DM's ID) adds the unread check on a
DM. Posts, reacts to and deletes its own messages there.
"""

from __future__ import annotations

import json
import os

import pytest

from aisquare_pipe_slack import SlackToolkit

USER_TOKEN = os.environ.get("SLACK_TEST_USER_TOKEN", "")
BOT_TOKEN = os.environ.get("SLACK_TEST_BOT_TOKEN", "")
CHANNEL = os.environ.get("SLACK_TEST_CHANNEL", "")
DM = os.environ.get("SLACK_TEST_DM", "")
ALL_TIERS = ["read", "send", "write", "destructive"]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not (USER_TOKEN and CHANNEL), reason="SLACK_TEST_USER_TOKEN / SLACK_TEST_CHANNEL not set"),
]


def _call(slug, args, **config):
    result = SlackToolkit().call_tool(slug, args, {"access_token": USER_TOKEN, "enabled_tiers": ALL_TIERS, **config})
    assert result.successful, result.error
    return result.data


def test_reads():
    assert _call("SLACK_TEST_AUTH", {})["user_id"]
    assert "label" in _call("SLACK_WHO_AM_I", {})
    assert "messages" in _call("SLACK_FETCH_CONVERSATION_HISTORY", {"channel": CHANNEL, "limit": 5})
    assert "messages" in _call("SLACK_SEARCH_MESSAGES", {"query": "the", "count": 1})


def test_unread_on_a_channel_works_or_says_why():
    # Slack documents last_read for DMs only; on a channel the tool must
    # either work or fail with the documented, non-retryable error.
    result = SlackToolkit().call_tool(
        "SLACK_LIST_UNREAD_CHANNEL_MESSAGES", {"channel": CHANNEL}, {"access_token": USER_TOKEN}
    )
    assert result.successful or (result.error_code == "invalid_arguments" and "last_read" in result.error), result.error


@pytest.mark.skipif(not DM, reason="SLACK_TEST_DM not set")
def test_unread_on_a_dm():
    assert "last_read" in _call("SLACK_LIST_UNREAD_CHANNEL_MESSAGES", {"channel": DM})


@pytest.mark.parametrize("name", ["pipe.ai (R&D)", "R_D *bot* <x>"])
def test_attribution_name_with_punctuation_shows_no_backslash(name):
    # The marker is escaped per format: in markdown_text, only markup
    # characters get a backslash. Slack must render the name without the
    # backslashes. If the second case fails, Slack doesn't honour CommonMark
    # escapes in markdown_text, and markdown_marker must avoid them.
    posted = _call("SLACK_SEND_MESSAGE", {"channel": CHANNEL, "markdown_text": "escaping check"},
                   attribution_name=name)
    try:
        shown = json.dumps({k: posted["message"].get(k) for k in ("text", "blocks")})
        assert "\\\\" not in shown, shown
        assert "Sent by" in shown
    finally:
        _call("SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", {"channel": CHANNEL, "ts": posted["ts"]})


def test_post_update_react_delete_as_the_user():
    posted = _call("SLACK_SEND_MESSAGE", {"channel": CHANNEL, "markdown_text": "pipe live test"})
    ts = posted["ts"]
    try:
        assert "Sent by pipe" in posted["message"]["text"]
        _call("SLACK_UPDATES_A_SLACK_MESSAGE", {"channel": CHANNEL, "ts": ts, "markdown_text": "pipe live test (edited)"})
        _call("SLACK_ADD_REACTION_TO_AN_ITEM", {"channel": CHANNEL, "timestamp": ts, "name": "white_check_mark"})
    finally:
        _call("SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", {"channel": CHANNEL, "ts": ts})


@pytest.mark.skipif(not BOT_TOKEN, reason="SLACK_TEST_BOT_TOKEN not set")
def test_post_and_delete_as_the_bot():
    config = {"access_token": BOT_TOKEN, "identity": "bot", "enabled_tiers": ALL_TIERS}
    toolkit = SlackToolkit()
    posted = toolkit.call_tool("SLACK_SEND_MESSAGE", {"channel": CHANNEL, "markdown_text": "pipe bot live test"}, config)
    assert posted.successful, posted.error
    deleted = toolkit.call_tool("SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", {"channel": CHANNEL, "ts": posted.data["ts"]}, config)
    assert deleted.successful, deleted.error
