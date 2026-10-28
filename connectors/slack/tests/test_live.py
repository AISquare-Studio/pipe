"""Live checks against a real Slack workspace — opt-in only.

Run with ``pytest -m live`` (or ``pipe validate --live``). Skipped unless
``SLACK_TEST_USER_TOKEN`` and ``SLACK_TEST_CHANNEL`` (a dedicated test
channel's ID, on the staging app) are set; ``SLACK_TEST_BOT_TOKEN`` adds the
bot-identity checks. Posts, reacts to and deletes its own messages there.
"""

from __future__ import annotations

import os

import pytest

from aisquare_pipe_slack import SlackToolkit

USER_TOKEN = os.environ.get("SLACK_TEST_USER_TOKEN", "")
BOT_TOKEN = os.environ.get("SLACK_TEST_BOT_TOKEN", "")
CHANNEL = os.environ.get("SLACK_TEST_CHANNEL", "")
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
