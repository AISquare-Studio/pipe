"""SlackToolkit behaviour beyond the per-tool table: risk tiers, identity,
attribution, channel names, composites, result shaping and secrecy."""

from __future__ import annotations

import json
import logging

import pytest
import requests
from aisquare.pipe import get_toolkit

from aisquare_pipe_slack import SlackToolkit
from aisquare_pipe_slack.constants import RESULT_CHAR_CAP
from aisquare_pipe_slack.tools import TOOLS, slim_message

from .fakes import (
    ALL_TIERS,
    BOT_CONFIG,
    BOT_TOKEN,
    USER_CONFIG,
    USER_TOKEN,
    channels_page,
    harness,
    ok,
    slack_error,
)

C = "C0123456"
TS = "1761312000.004100"

READ = {
    "SLACK_LIST_SCHEDULED_MESSAGES", "SLACK_RETRIEVE_MESSAGE_PERMALINK_URL",
    "SLACK_FETCH_CONVERSATION_HISTORY", "SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION",
    "SLACK_LIST_UNREAD_CHANNEL_MESSAGES", "SLACK_SEARCH_MESSAGES", "SLACK_SEARCH_FILES",
    "SLACK_SEARCH_ALL", "SLACK_FIND_CHANNELS", "SLACK_FIND_USERS", "SLACK_LIST_ALL_CHANNELS",
    "SLACK_LIST_CONVERSATIONS", "SLACK_RETRIEVE_CONVERSATION_INFORMATION",
    "SLACK_RETRIEVE_CONVERSATION_MEMBERS_LIST", "SLACK_FETCH_ITEM_REACTIONS",
    "SLACK_LIST_USER_REACTIONS", "SLACK_LIST_PINNED_ITEMS", "SLACK_LIST_ALL_USERS",
    "SLACK_RETRIEVE_DETAILED_USER_INFORMATION", "SLACK_FIND_USER_BY_EMAIL_ADDRESS",
    "SLACK_RETRIEVE_USER_PROFILE_INFORMATION", "SLACK_GET_USER_PRESENCE", "SLACK_FETCH_TEAM_INFO",
    "SLACK_LIST_CUSTOM_EMOJIS", "SLACK_TEST_AUTH", "SLACK_WHO_AM_I", "SLACK_API_TEST",
}
SEND = {"SLACK_SEND_MESSAGE", "SLACK_SEND_EPHEMERAL_MESSAGE", "SLACK_SCHEDULE_MESSAGE", "SLACK_OPEN_DM"}
DESTRUCTIVE = {
    "SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", "SLACK_DELETE_SCHEDULED_MESSAGE",
    "SLACK_REMOVE_USER_FROM_CONVERSATION", "SLACK_ARCHIVE_CONVERSATION",
}
WRITE = {
    "SLACK_UPDATES_A_SLACK_MESSAGE", "SLACK_CREATE_CHANNEL", "SLACK_JOIN_AN_EXISTING_CONVERSATION",
    "SLACK_LEAVE_CONVERSATION", "SLACK_INVITE_USERS_TO_A_SLACK_CHANNEL", "SLACK_UNARCHIVE_CHANNEL",
    "SLACK_RENAME_CONVERSATION", "SLACK_SET_THE_TOPIC_OF_A_CONVERSATION",
    "SLACK_SET_CONVERSATION_PURPOSE", "SLACK_CLOSE_DM", "SLACK_ADD_REACTION_TO_AN_ITEM",
    "SLACK_REMOVE_REACTION_FROM_ITEM", "SLACK_PIN_ITEM", "SLACK_UNPIN_ITEM",
    "SLACK_SET_USER_PROFILE", "SLACK_SET_USER_PRESENCE",
}
USER_ONLY = {
    "SLACK_SEARCH_MESSAGES", "SLACK_SEARCH_FILES", "SLACK_SEARCH_ALL",
    "SLACK_LIST_UNREAD_CHANNEL_MESSAGES", "SLACK_SET_USER_PROFILE", "SLACK_UNARCHIVE_CHANNEL",
}
SEND_ARGS = {"channel": C, "markdown_text": "hello"}


class TestCatalog:
    def test_registered_as_the_slack_toolkit(self):
        assert get_toolkit("slack") is SlackToolkit

    def test_identity(self):
        toolkit = SlackToolkit()
        assert (toolkit.name, toolkit.version, toolkit.slug_prefix) == ("slack", "0.1.0", "SLACK_")
        assert len(toolkit.tools()) == 51

    def test_every_tool_is_in_its_tier(self):
        assert READ | SEND | WRITE | DESTRUCTIVE == set(TOOLS)
        for slugs, tier in ((READ, "read"), (SEND, "send"), (WRITE, "write"), (DESTRUCTIVE, "destructive")):
            assert {s for s, t in TOOLS.items() if t.tier == tier} == slugs

    def test_flags_follow_the_tiers(self):
        for slug, tool in TOOLS.items():
            assert tool.spec.read_only is (tool.tier == "read"), slug
            # An edit overwrites content: destructive by core's definition, but
            # in the opt-in write tier by the owner's.
            overwrites = slug == "SLACK_UPDATES_A_SLACK_MESSAGE"
            assert tool.spec.destructive is (tool.tier == "destructive" or overwrites), slug

    def test_user_only_tools(self):
        assert {s for s, t in TOOLS.items() if t.identities == ("user",)} == USER_ONLY

    def test_tier_of(self):
        assert SlackToolkit.tier_of("slack_archive_conversation") == "destructive"
        assert SlackToolkit.tier_of("SLACK_NOPE") is None


class TestTiers:
    def test_default_lists_read_and_send_only(self):
        listed = {t.slug for t in SlackToolkit().tools_for(USER_CONFIG)}
        assert listed == READ | SEND

    def test_disabled_tool_is_forbidden_without_a_request(self):
        h = harness()
        result = h.call("SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", {"channel": C, "ts": TS})
        assert result.successful is False
        assert result.error_code == "forbidden"
        assert "'destructive' tool" in result.error
        assert "config['enabled_tiers']" in result.error
        assert "'SLACK_DELETES_A_MESSAGE_FROM_A_CHAT' to config['enabled_tools']" in result.error
        assert h.session.calls == []

    def test_forbidden_comes_before_argument_checks(self):
        result = harness().call("SLACK_ARCHIVE_CONVERSATION", {})
        assert result.error_code == "forbidden"

    @pytest.mark.parametrize("slug", sorted(WRITE | DESTRUCTIVE))
    def test_every_risky_tool_is_off_by_default(self, slug):
        assert harness().call(slug, {}).error_code == "forbidden"

    def test_opt_in_by_tier(self):
        h = harness(ok())
        tiers = ["read", "send", "write"]
        args = {"channel": C, "timestamp": TS, "name": "eyes"}
        assert h.call("SLACK_ADD_REACTION_TO_AN_ITEM", args, enabled_tiers=tiers).successful
        deleted = h.call("SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", {"channel": C, "ts": TS}, enabled_tiers=tiers)
        assert deleted.error_code == "forbidden"
        assert {t.slug for t in SlackToolkit().tools_for({**USER_CONFIG, "enabled_tiers": tiers})} == (
            READ | SEND | WRITE
        )

    def test_opt_in_by_slug(self):
        h = harness(ok())
        config = {**USER_CONFIG, "enabled_tools": ["slack_pin_item"]}
        assert h.call("SLACK_PIN_ITEM", {"channel": C, "timestamp": TS}, config).successful
        assert h.call("SLACK_UNPIN_ITEM", {"channel": C, "timestamp": TS}, config).error_code == "forbidden"
        assert {t.slug for t in SlackToolkit().tools_for(config)} == READ | SEND | {"SLACK_PIN_ITEM"}

    def test_comma_separated_names_are_accepted(self):
        config = {**USER_CONFIG, "enabled_tiers": "read, write"}
        assert {t.slug for t in SlackToolkit().tools_for(config)} == READ | WRITE

    def test_enabled_tiers_replaces_the_default(self):
        h = harness()
        assert h.call("SLACK_SEND_MESSAGE", SEND_ARGS, enabled_tiers=["read"]).error_code == "forbidden"

    @pytest.mark.parametrize("tiers", [["*"], ["all"], ["read", "destrucive"]])
    def test_no_wildcards_and_typos_fail_closed(self, tiers):
        h = harness()
        result = h.call("SLACK_TEST_AUTH", {}, enabled_tiers=tiers)
        assert result.successful is False
        assert result.error_code == "upstream_error"
        assert "config['enabled_tiers']" in result.error
        assert h.session.calls == []
        assert SlackToolkit().tools_for({**USER_CONFIG, "enabled_tiers": tiers}) == []

    @pytest.mark.parametrize(
        "key, value", [("enabled_tiers", 3), ("enabled_tools", [1]), ("bot_attribution", "yes"), ("identity", "app")]
    )
    def test_bad_config_values_name_their_key(self, key, value):
        result = harness().call("SLACK_TEST_AUTH", {}, **{key: value})
        assert result.error_code == "upstream_error"
        assert f"config[{key!r}]" in result.error


class TestIdentity:
    @pytest.mark.parametrize("config, token", [(USER_CONFIG, USER_TOKEN), (BOT_CONFIG, BOT_TOKEN)])
    def test_send_works_with_either_identity(self, config, token):
        h = harness(ok(channel=C, ts="1761400000.000200"))
        result = h.call("SLACK_SEND_MESSAGE", SEND_ARGS, config)
        assert result.successful, result.error
        assert result.data == {"channel": C, "ts": "1761400000.000200"}
        assert h.session.last.headers["Authorization"] == f"Bearer {token}"
        assert h.session.last.api_method == "chat.postMessage"

    @pytest.mark.parametrize("config, token", [(USER_CONFIG, USER_TOKEN), (BOT_CONFIG, BOT_TOKEN)])
    def test_read_works_with_either_identity(self, config, token):
        h = harness(ok(messages=[{"ts": TS, "text": "hi"}]))
        result = h.call("SLACK_FETCH_CONVERSATION_HISTORY", {"channel": C}, config)
        assert result.successful, result.error
        assert result.data["messages"] == [{"ts": TS, "text": "hi"}]
        assert h.session.last.headers["Authorization"] == f"Bearer {token}"

    @pytest.mark.parametrize("slug", sorted(USER_ONLY & READ))
    def test_user_only_tool_under_the_bot_is_forbidden_without_a_request(self, slug):
        h = harness()
        result = h.call(slug, {"query": "x", "channel": C}, BOT_CONFIG)
        assert result.error_code == "forbidden"
        assert "needs the user identity" in result.error
        assert "config['identity']" in result.error
        assert h.session.calls == []

    def test_bot_listing_drops_user_only_tools(self):
        listed = {t.slug for t in SlackToolkit().tools_for({**BOT_CONFIG, "enabled_tiers": ALL_TIERS})}
        assert listed == set(TOOLS) - USER_ONLY

    def test_wrong_token_type_names_the_identity_key(self):
        h = harness(slack_error("not_allowed_token_type"))
        result = h.call("SLACK_SEND_MESSAGE", SEND_ARGS, BOT_CONFIG)
        assert result.error_code == "forbidden"
        assert "does not accept a bot token" in result.error
        assert "the user identity (config['identity'])" in result.error

    def test_who_am_i_reports_the_identity(self):
        h = harness(ok(user="aisquare", user_id="U0BOT1234", bot_id="B0123", team="Acme"), slack_error("missing_scope"))
        result = h.call("SLACK_WHO_AM_I", {}, BOT_CONFIG)
        assert result.successful, result.error
        assert result.data["identity"] == "bot"
        assert result.data["label"] == "aisquare @ Acme"


class TestAttribution:
    def _sent(self, args, slug="SLACK_SEND_MESSAGE", config=None, **overrides):
        h = harness(ok(channel=C, ts=TS))
        result = h.call(slug, args, config, enabled_tiers=ALL_TIERS, **overrides)
        assert result.successful, result.error
        return h.session.last.args

    def test_user_messages_carry_the_default_marker(self):
        assert self._sent(SEND_ARGS)["markdown_text"] == "hello\n\n_Sent by pipe_"

    def test_custom_name(self):
        sent = self._sent(SEND_ARGS, attribution_name="  pipe   by aisquare ")
        assert sent["markdown_text"] == "hello\n\n_Sent by pipe by aisquare_"

    def test_name_is_escaped_and_blank_falls_back(self):
        assert self._sent(SEND_ARGS, attribution_name="<!channel> & co")["markdown_text"].endswith(
            "_Sent by &lt;!channel&gt; &amp; co_"
        )
        assert self._sent(SEND_ARGS, attribution_name="   ")["markdown_text"].endswith("_Sent by pipe_")

    def test_blocks_get_a_context_block(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
        sent = self._sent({"channel": C, "blocks": blocks, "fallback_text": "hi"})
        assert sent["blocks"] == [
            *blocks,
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Sent by pipe"}]},
        ]
        assert sent["text"] == "hi"

    def test_blocks_may_come_as_a_json_string(self):
        blocks = [{"type": "divider"}]
        sent = self._sent({"channel": C, "blocks": json.dumps(blocks)}, attribution_name="aisquare")
        assert sent["blocks"][0] == {"type": "divider"}
        assert sent["blocks"][1]["elements"][0]["text"] == "Sent by aisquare"

    def test_plain_text_gets_a_last_line(self):
        sent = self._sent({"channel": C, "user": "U0123456", "text": "psst"}, "SLACK_SEND_EPHEMERAL_MESSAGE")
        assert sent["text"] == "psst\n_Sent by pipe_"

    def test_edits_keep_the_marker(self):
        sent = self._sent({"channel": C, "ts": TS, "markdown_text": "fixed"}, "SLACK_UPDATES_A_SLACK_MESSAGE")
        assert sent["markdown_text"] == "fixed\n\n_Sent by pipe_"

    def test_bot_messages_have_no_marker_by_default(self):
        assert self._sent(SEND_ARGS, config=BOT_CONFIG)["markdown_text"] == "hello"
        blocks = [{"type": "divider"}]
        assert self._sent({"channel": C, "blocks": blocks}, config=BOT_CONFIG)["blocks"] == blocks

    def test_bot_marker_can_be_enabled(self):
        sent = self._sent(SEND_ARGS, config=BOT_CONFIG, bot_attribution=True, attribution_name="aisquare")
        assert sent["markdown_text"] == "hello\n\n_Sent by aisquare_"

    def test_a_full_block_list_leaves_no_room_for_the_marker(self):
        blocks = [{"type": "divider"}] * 50
        h = harness()
        result = h.call("SLACK_SEND_MESSAGE", {"channel": C, "blocks": blocks})
        assert result.error_code == "invalid_arguments"
        assert "at most 49 blocks" in result.error
        assert h.session.calls == []
        assert self._sent({"channel": C, "blocks": blocks}, config=BOT_CONFIG)["blocks"] == blocks

    @pytest.mark.parametrize(
        "args, expected",
        [
            ({"channel": C, "markdown_text": "a", "blocks": [{"type": "divider"}]}, "cannot be combined"),
            ({"channel": C}, "message content is missing"),
            ({"channel": C, "markdown_text": "  "}, "markdown_text is empty"),
            ({"channel": C, "blocks": "not json"}, "not valid JSON"),
            ({"channel": C, "blocks": [1, 2]}, "must be object"),
            ({"channel": C, "blocks": "[1]"}, "array of Block Kit block objects"),
        ],
    )
    def test_bad_content_fails_before_any_request(self, args, expected):
        h = harness()
        result = h.call("SLACK_SEND_MESSAGE", args)
        assert result.error_code == "invalid_arguments"
        assert expected in result.error
        assert h.session.calls == []


class TestChannelNames:
    def test_a_name_is_looked_up_then_the_id_used(self):
        h = harness(channels_page({"id": "C0OTHER01", "name": "random"}, {"id": C, "name": "general"}), ok())
        assert h.call("SLACK_SEND_MESSAGE", {"channel": "#General", "markdown_text": "hi"}).successful
        lookup, post = h.session.calls
        assert lookup.api_method == "conversations.list"
        assert lookup.args == {
            "types": "public_channel,private_channel",
            "exclude_archived": "true",
            "limit": "200",
        }
        assert post.args["channel"] == C

    def test_the_lookup_follows_the_cursor(self):
        h = harness(
            channels_page({"id": "C0OTHER01", "name": "random"}, cursor="next"),
            channels_page({"id": C, "name": "general"}),
            ok(messages=[]),
        )
        assert h.call("SLACK_FETCH_CONVERSATION_HISTORY", {"channel": "general"}).successful
        assert h.session.calls[1].args["cursor"] == "next"
        assert h.session.last.args["channel"] == C

    def test_an_ambiguous_name_is_refused_before_any_write(self):
        h = harness(channels_page({"id": C, "name": "general"}, {"id": "C0GRIDWS2", "name": "general"}))
        result = h.call("SLACK_SEND_MESSAGE", {"channel": "general", "markdown_text": "hi"})
        assert result.error_code == "invalid_arguments"
        assert "matches 2 conversations" in result.error
        assert h.session.methods == ["conversations.list"]

    def test_an_unknown_name_is_not_found(self):
        h = harness(channels_page({"id": C, "name": "general"}))
        result = h.call("SLACK_SEND_MESSAGE", {"channel": "#nope", "markdown_text": "hi"})
        assert result.error_code == "not_found"
        assert "no channel named '#nope'" in result.error

    def test_unarchive_looks_among_archived_channels(self):
        h = harness(channels_page({"id": C, "name": "old", "is_archived": True}), ok())
        assert h.call("SLACK_UNARCHIVE_CHANNEL", {"channel": "old"}, enabled_tiers=ALL_TIERS).successful
        assert h.session.calls[0].args["exclude_archived"] == "false"

    @pytest.mark.parametrize("channel", ["C0123456", "G0123456", "D0123456", "U0123456"])
    def test_ids_pass_through(self, channel):
        h = harness(ok())
        assert h.call("SLACK_SEND_MESSAGE", {"channel": channel, "markdown_text": "hi"}).successful
        assert h.session.methods == ["chat.postMessage"]


class TestComposites:
    def test_find_channels_matches_name_topic_and_purpose(self):
        h = harness(
            channels_page(
                {"id": "C01", "name": "product", "is_member": True, "topic": {"value": ""}},
                {"id": "C02", "name": "launch", "topic": {"value": "Product launch"}, "purpose": {"value": ""}},
                {"id": "C03", "name": "random", "purpose": {"value": "anything"}},
            )
        )
        result = h.call("SLACK_FIND_CHANNELS", {"query": "#Product"})
        assert [c["id"] for c in result.data["channels"]] == ["C01", "C02"]
        assert result.data["channels"][1] == {"id": "C02", "name": "launch", "topic": "Product launch"}

    def test_find_channels_exact_and_member_only(self):
        page = channels_page(
            {"id": "C01", "name": "product", "is_member": False},
            {"id": "C02", "name": "product-eng", "is_member": True},
        )
        assert [c["id"] for c in harness(page).call(
            "SLACK_FIND_CHANNELS", {"query": "product", "exact_match": True}
        ).data["channels"]] == ["C01"]
        page = channels_page(
            {"id": "C01", "name": "product", "is_member": False},
            {"id": "C02", "name": "product-eng", "is_member": True},
        )
        assert [c["id"] for c in harness(page).call(
            "SLACK_FIND_CHANNELS", {"query": "product", "member_only": True}
        ).data["channels"]] == ["C02"]

    def test_find_channels_stops_at_the_page_cap(self):
        pages = [channels_page({"id": f"C0{i}", "name": f"c{i}"}, cursor=f"n{i}") for i in range(10)]
        result = harness(*pages).call("SLACK_FIND_CHANNELS", {"query": "zzz"})
        assert result.data == {"has_more": True, "next_cursor": "n9", "truncated": True, "channels": []}

    def test_find_users_by_email_and_by_id(self):
        h = harness(ok(user={"id": "U0123456"}))
        assert h.call("SLACK_FIND_USERS", {"email": "sam@acme.test"}).data["members"] == [{"id": "U0123456"}]
        assert h.session.last.api_method == "users.lookupByEmail"
        h = harness(ok(user={"id": "U0123456"}))
        assert h.call("SLACK_FIND_USERS", {"search_query": "@U0123456"}).successful
        assert h.session.last.api_method == "users.info"

    def test_find_users_filters(self):
        members = [
            {"id": "U01", "name": "sam", "profile": {"display_name": "Sam", "image_72": "x"}},
            {"id": "U02", "name": "samantha", "deleted": True},
            {"id": "U03", "name": "sambot", "is_bot": True},
            {"id": "U04", "name": "sam-guest", "is_restricted": True},
            {"id": "U05", "name": "alex", "real_name": "Alex Samson"},
        ]
        result = harness(ok(members=members)).call("SLACK_FIND_USERS", {"search_query": "Sam"})
        assert [u["id"] for u in result.data["members"]] == ["U01", "U05"]
        assert result.data["members"][0] == {"id": "U01", "name": "sam", "profile": {"display_name": "Sam"}}
        everyone = {"include_deleted": True, "include_bots": True, "include_restricted": True}
        result = harness(ok(members=members)).call("SLACK_FIND_USERS", {"search_query": "sam", **everyone})
        assert len(result.data["members"]) == 5
        result = harness(ok(members=members)).call("SLACK_FIND_USERS", {"search_query": "sam", "exact_match": True})
        assert [u["id"] for u in result.data["members"]] == ["U01"]

    def test_find_users_needs_a_query(self):
        assert harness().call("SLACK_FIND_USERS", {}).error_code == "invalid_arguments"

    def test_unread_without_a_read_position(self):
        result = harness(ok(channel={"id": C})).call("SLACK_LIST_UNREAD_CHANNEL_MESSAGES", {"channel": C})
        assert result.error_code == "upstream_error"
        assert "last_read" in result.error

    def test_unread_reports_the_read_position(self):
        h = harness(ok(channel={"id": C, "last_read": "1.5", "unread_count_display": 3}), ok(messages=[]))
        result = h.call("SLACK_LIST_UNREAD_CHANNEL_MESSAGES", {"channel": C})
        assert (result.data["last_read"], result.data["unread_count_display"]) == ("1.5", 3)

    def test_who_am_i_surfaces_other_team_info_failures(self):
        result = harness(ok(user="sam"), slack_error("invalid_auth")).call("SLACK_WHO_AM_I", {})
        assert result.error_code == "unauthorized"

    def test_open_dm_takes_an_array_and_needs_someone(self):
        h = harness(ok(channel={"id": "G0123456"}))
        assert h.call("SLACK_OPEN_DM", {"users": ["U01", " U02 "]}).successful
        assert h.session.last.args == {"users": "U01,U02"}
        assert harness().call("SLACK_OPEN_DM", {}).error_code == "invalid_arguments"

    @pytest.mark.parametrize(
        "args, sent",
        [
            ({"name": "title", "value": "CTO"}, {"name": "title", "value": "CTO"}),
            ({"profile": '{"status_text": "Out"}'}, {"profile": {"status_text": "Out"}}),
        ],
    )
    def test_set_user_profile_shapes(self, args, sent):
        h = harness(ok())
        assert h.call("SLACK_SET_USER_PROFILE", args, enabled_tiers=ALL_TIERS).successful
        assert h.session.last.args == sent

    @pytest.mark.parametrize(
        "args", [{}, {"profile": "nope"}, {"profile": "[1]"}, {"profile": {"a": 1}, "name": "a", "value": "b"}]
    )
    def test_set_user_profile_refuses_bad_shapes(self, args):
        assert harness().call("SLACK_SET_USER_PROFILE", args, enabled_tiers=ALL_TIERS).error_code == (
            "invalid_arguments"
        )

    @pytest.mark.parametrize("post_at", ["tomorrow", "-5", "1.5", True])
    def test_schedule_needs_a_unix_time(self, post_at):
        result = harness().call("SLACK_SCHEDULE_MESSAGE", {"channel": C, "post_at": post_at, "text": "x"})
        assert result.error_code == "invalid_arguments"

    @pytest.mark.parametrize("slug", ["SLACK_REMOVE_REACTION_FROM_ITEM", "SLACK_FETCH_ITEM_REACTIONS"])
    def test_reactions_need_a_target(self, slug):
        result = harness().call(slug, {"name": "eyes", "channel": C}, enabled_tiers=ALL_TIERS)
        assert result.error_code == "invalid_arguments"


class TestResultShaping:
    def test_slim_message(self):
        message = {
            "ts": TS,
            "text": "hi",
            "blocks": [{"type": "rich_text", "elements": []}],
            "user_profile": {"real_name": "Sam"},
            "bot_profile": {"id": "B01", "icons": {"image_36": "x"}},
        }
        assert slim_message(message) == {"ts": TS, "text": "hi", "bot_profile": {"id": "B01"}}
        designed = {"ts": TS, "text": "hi", "blocks": [{"type": "section"}]}
        assert slim_message(designed) == designed

    def test_a_long_history_is_cut_with_a_resume_hint(self):
        messages = [{"ts": f"17610000{i:02d}.000100", "text": "x" * 9_000} for i in range(15)]
        h = harness(ok(messages=messages, response_metadata={"next_cursor": "more"}))
        result = h.call("SLACK_FETCH_CONVERSATION_HISTORY", {"channel": C})
        data = result.data
        assert list(data)[:4] == ["has_more", "next_cursor", "truncated", "resume_latest"]
        kept = data["messages"]
        assert 0 < len(kept) < 15
        assert data["resume_latest"] == kept[-1]["ts"]
        assert len(json.dumps(data)) <= RESULT_CHAR_CAP

    def test_a_long_thread_resumes_from_the_oldest_side(self):
        messages = [{"ts": f"17610000{i:02d}.000100", "text": "x" * 9_000} for i in range(15)]
        result = harness(ok(messages=messages)).call(
            "SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION", {"channel": C, "ts": TS}
        )
        assert result.data["resume_oldest"] == result.data["messages"][-1]["ts"]

    def test_limits_are_clamped(self):
        h = harness(ok(messages=[]))
        h.call("SLACK_FETCH_CONVERSATION_HISTORY", {"channel": C, "limit": 5000})
        assert h.session.last.args["limit"] == "200"
        h = harness(ok(messages={"matches": []}))
        h.call("SLACK_SEARCH_MESSAGES", {"query": "x", "count": 0})
        assert h.session.last.args["count"] == "1"

    def test_search_matches_are_slimmed(self):
        match = {"ts": TS, "text": "hi", "blocks": [{"type": "rich_text"}], "user_profile": {}}
        result = harness(ok(messages={"matches": [match]})).call("SLACK_SEARCH_MESSAGES", {"query": "hi"})
        assert result.data["messages"]["matches"] == [{"ts": TS, "text": "hi"}]

    def test_search_cursormark(self):
        reply = ok(messages={"matches": [], "pagination": {"next_cursor": "mark2"}})
        result = harness(reply).call("SLACK_SEARCH_MESSAGES", {"query": "x", "cursor": "*"})
        assert (result.data["has_more"], result.data["next_cursor"]) == (True, "mark2")


class TestSecrecy:
    def test_token_echoed_in_an_error_is_scrubbed(self):
        result = harness(slack_error(f"weird {USER_TOKEN}")).call("SLACK_TEST_AUTH", {})
        assert USER_TOKEN not in (result.error or "")

    def test_network_error_carries_no_url_or_arguments(self):
        failure = requests.ConnectionError("https://slack.com/api/chat.postMessage?text=secret-plan")
        result = harness(failure).call("SLACK_SEND_MESSAGE", {"channel": C, "markdown_text": "secret-plan"})
        assert result.error_code == "upstream_error"
        assert result.error == "chat.postMessage: network_error — could not reach Slack"

    def test_logs_carry_no_token_or_arguments(self, caplog):
        caplog.set_level(logging.DEBUG)
        h = harness(slack_error("ratelimited"), ok())
        h.call("SLACK_SEND_MESSAGE", {"channel": C, "markdown_text": "secret-plan"})
        text = caplog.text
        assert USER_TOKEN not in text and "secret-plan" not in text
        assert "chat.postMessage" in text
