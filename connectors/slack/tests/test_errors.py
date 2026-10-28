"""Slack failures -> canonical error codes and messages (plan §4.4): table
driven over every code, then the transport/HTTP order, the hints, and what a
message must never carry."""

from __future__ import annotations

import pytest

from aisquare_pipe_slack.config import SlackSettings
from aisquare_pipe_slack.rest import SlackAPIError
from aisquare_pipe_slack.toolkit import describe_error, error_code, failure

from .fakes import USER_TOKEN, harness, ok, response, slack_error

TABLE = {
    "unauthorized": ["not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired"],
    "forbidden": [
        "missing_scope", "no_permission", "not_allowed_token_type", "restricted_action", "not_authorized",
        "ekm_access_denied", "team_access_not_granted", "enterprise_is_restricted", "accesslimited",
        "two_factor_setup_required", "cant_update_message", "cant_delete_message", "not_in_channel",
        "plan_upgrade_required", "paid_only", "permission_denied", "edit_window_closed",
        "message_limit_exceeded", "messages_tab_disabled",
        # S2
        "cant_delete_file", "cant_delete", "delete_not_allowed", "file_deleting_disabled",
        "canvas_deleting_disabled", "canvas_disabled_user_team", "canvas_disabled_file_team",
        "canvas_globally_disabled", "free_teams_cannot_create_standalone_canvases",
        "free_teams_cannot_edit_standalone_canvases", "team_tier_cannot_create_channel_canvases",
        "paid_teams_only", "not_allowed", "public_file_type_not_allowed", "public_video_not_allowed",
        "file_uploads_disabled", "file_uploads_except_images_disabled", "file_type_not_allowed",
        "file_upload_size_restricted", "storage_limit_reached", "posting_to_channel_denied",
        "user_is_external_guest", "cannot_bookmark_from_external_org",
        "cannot_bookmark_restricted_sharing_enabled", "slack_connect_canvas_sharing_blocked",
        "slack_connect_file_upload_sharing_blocked", "slack_connect_blocked_file_type",
        "cannot_modify_role_admin", "use_skills_api",
    ],
    "not_found": [
        "channel_not_found", "user_not_found", "users_not_found", "message_not_found", "thread_not_found",
        "file_not_found", "canvas_not_found", "bookmark_not_found", "no_such_subteam", "team_not_found",
        "not_found",
        "bot_not_found", "bots_not_found", "file_deleted", "canvas_deleted", "not_visible", "template_not_visible",
    ],
    "conflict": [
        "already_reacted", "no_reaction", "already_pinned", "not_pinned", "name_taken", "is_archived",
        "already_archived", "not_archived", "already_in_channel", "channel_canvas_already_exists",
        "is_inactive", "too_many_pins", "too_many_reactions",
        "name_already_exists", "handle_already_exists", "file_already_added", "too_many_bookmarks",
        "too_many_tabs", "free_team_canvas_tab_already_exists", "not_in_dnd", "subteam_max_users_exceeded",
    ],
    "rate_limited": ["ratelimited", "rate_limited", "restricted_too_many", "too_many_requests",
                     "canvas_editing_locked"],
    "invalid_arguments": [
        "invalid_arguments", "invalid_arg_name", "invalid_cursor", "invalid_ts_latest", "invalid_ts_oldest",
        "msg_too_long", "msg_blocks_too_long", "no_text", "invalid_blocks", "invalid_name",
        "invalid_name_punctuation", "time_in_past", "time_too_far", "too_many_users", "user_not_in_channel",
        "invalid_user", "snippet_too_large", "invalid_charset", "invalid_form_data", "invalid_post_type",
        "missing_post_type", "markdown_text_conflict", "bad_timestamp", "no_item_specified",
        "cant_invite_self",
        "invalid_timestamp", "invalid_link", "invalid_emoji", "invalid_bookmark_type", "invalid_entity_id",
        "bad_image", "too_large", "too_many_frames", "missing_duration", "missing_argument", "cannot_parse",
        "cannot_add_others", "cannot_add_others_recurring", "cannot_add_bot", "cannot_add_slackbot",
        "unknown_type", "unknown_snippet_type", "alt_txt_too_large", "bad_handle", "forbidden_handle",
        "description_too_long", "name_too_long", "missing_subteam_name", "invalid_channel_provided",
        "no_users_provided", "channels_limit_exceeded", "invalid_parameters", "invalid_channel",
        "canvas_too_large", "file_too_large",
    ],
    "upstream_error": [
        "fatal_error", "internal_error", "service_unavailable", "request_timeout", "something_new_from_slack",
        "canvas_creation_failed", "canvas_editing_failed", "canvas_tab_creation_failed", "file_update_failed",
        "snooze_failed", "snooze_end_failed",
    ],
}
ROWS = [(code, kind) for kind, codes in TABLE.items() for code in codes]


def _error(code: str = "x", **kwargs) -> SlackAPIError:
    kwargs.setdefault("status", 200)
    return SlackAPIError("chat.postMessage", code, **kwargs)


@pytest.mark.parametrize("code, kind", ROWS, ids=[c for c, _ in ROWS])
def test_every_slack_code_maps(code, kind):
    assert error_code(_error(code)) == kind


@pytest.mark.parametrize("code, kind", ROWS[::5], ids=[c for c, _ in ROWS[::5]])
def test_end_to_end_through_call_tool(code, kind):
    # Three copies: rate limits and (for this read) server errors are retried.
    result = harness(*[slack_error(code) for _ in range(3)]).call("SLACK_TEST_AUTH", {})
    assert (result.successful, result.error_code) == (False, kind)
    assert result.data == {"slack_error": code, "status": 200}


def test_missing_scope_is_forbidden_not_unauthorized():
    # An "unauthorized" would send the host into a refresh/reconnect loop.
    result = harness(slack_error("missing_scope", needed="reactions:write")).call("SLACK_TEST_AUTH", {})
    assert result.error_code == "forbidden"
    assert result.error == "auth.test: missing_scope — reconnect Slack to grant `reactions:write`"
    result = harness(slack_error("missing_scope")).call("SLACK_TEST_AUTH", {})
    assert result.error.endswith("reconnect Slack to grant the missing scope")


@pytest.mark.parametrize(
    "error, kind",
    [
        (_error("timeout", status=0, timed_out=True), "timeout"),
        (_error("network_error", status=0), "upstream_error"),
        (_error("ratelimited", status=429, retry_after=5), "rate_limited"),
        (_error("http_429", status=429), "rate_limited"),
        (_error("http_408", status=408), "timeout"),
        (_error("http_504", status=504), "timeout"),
        (_error("http_502", status=502), "upstream_error"),
        (_error("http_401", status=401), "unauthorized"),
        (_error("http_403", status=403), "forbidden"),
        (_error("http_404", status=404), "not_found"),
        (_error("http_400", status=400), "invalid_arguments"),
        (_error("http_302", status=302), "upstream_error"),
        # HTTP status wins over the body code.
        (_error("channel_not_found", status=500), "upstream_error"),
    ],
)
def test_transport_and_http_status_come_first(error, kind):
    assert error_code(error) == kind


@pytest.mark.parametrize(
    "error, text",
    [
        (_error("already_reacted"), "already in that state; no change needed"),
        (_error("is_archived"), "the channel is archived; nothing can be posted"),
        (_error("is_inactive"), "archived, frozen or deleted"),
        (_error("name_taken"), "pick another name"),
        (_error("too_many_pins"), "limit on pins"),
        (_error("too_many_reactions"), "limit on reactions"),
        (_error("edit_window_closed"), "no longer allow editing"),
        (_error("message_limit_exceeded"), "plan's message limit"),
        (_error("messages_tab_disabled"), "Messages tab is turned off"),
        (_error("bad_timestamp"), "pass ts exactly as Slack returned it"),
        (_error("channel_canvas_already_exists"), "already has its canvas"),
        (_error("invalid_cursor"), "start again without it"),
        (_error("time_in_past"), "post_at must be in the future"),
        (_error("msg_too_long"), "shorten it"),
        (_error("no_text"), "check the arguments"),
        (_error("internal_error"), "Slack had a problem"),
        (_error("ratelimited", retry_after=61.2), "retry in about 62s"),
        (_error("restricted_too_many"), "wait before retrying"),
        (_error("no_permission"), "not allowed to do that"),
        (_error("brand_new_code"), "chat.postMessage: brand_new_code"),
    ],
)
def test_hints(error, text):
    assert text in describe_error(error)


def test_not_in_channel_points_to_join_only_when_it_is_enabled():
    error = _error("not_in_channel")
    off = describe_error(error)  # default tiers: read + send
    assert "ask a member to invite it" in off
    assert "'write' tool that is not enabled here" in off
    assert "config['enabled_tiers'] or config['enabled_tools']" in off
    for settings in (
        SlackSettings(enabled_tiers=frozenset({"read", "send", "write"})),
        SlackSettings(enabled_tools=frozenset({"SLACK_JOIN_AN_EXISTING_CONVERSATION"})),
    ):
        on = describe_error(error, settings)
        assert "join it first with SLACK_JOIN_AN_EXISTING_CONVERSATION" in on
        assert "not enabled" not in on


def test_not_in_channel_end_to_end_follows_the_config():
    args = {"channel": "C0123456", "markdown_text": "hi"}
    assert "not enabled here" in harness(slack_error("not_in_channel")).call("SLACK_SEND_MESSAGE", args).error
    enabled = harness(slack_error("not_in_channel")).call(
        "SLACK_SEND_MESSAGE", args, enabled_tiers=["read", "send", "write"]
    )
    assert "join it first" in enabled.error


NO_CHANGE = ["already_reacted", "no_reaction", "already_pinned", "not_pinned", "already_archived",
             "not_archived", "already_in_channel"]


@pytest.mark.parametrize("code", [c for c, _ in ROWS])
def test_no_change_needed_only_where_the_change_is_in_place(code):
    # A failed action must never read as done.
    assert ("no change needed" in describe_error(_error(code))) is (code in NO_CHANGE)


def test_posting_to_an_archived_channel_does_not_read_as_done():
    result = harness(slack_error("is_archived")).call("SLACK_SEND_MESSAGE", {"channel": "C0123456", "markdown_text": "hi"})
    assert result.error_code == "conflict"
    assert "no change needed" not in result.error
    assert "archived" in result.error


def test_the_token_type_hint_does_not_assume_the_identity_matches():
    for settings in (SlackSettings(), SlackSettings(identity="bot")):
        message = describe_error(_error("not_allowed_token_type"), settings)
        assert "Check that config['identity']" in message
        assert f"(now {settings.identity!r})" in message


@pytest.mark.parametrize("code", ["too_many_requests", "canvas_editing_locked"])
def test_retry_soon_codes_are_retried_after_a_short_wait(code):
    h = harness(slack_error(code), ok())
    assert h.call("SLACK_TEST_AUTH", {}).successful
    assert h.sleeps == [2.0]
    limited = harness(*[slack_error(code) for _ in range(3)]).call("SLACK_TEST_AUTH", {})
    assert (limited.error_code, limited.retry_after) == ("rate_limited", 2.0)


@pytest.mark.parametrize(
    "code, text",
    [
        ("name_already_exists", "pick another name"),
        ("too_many_bookmarks", "limit of 100 bookmarks"),
        ("bad_image", "512-1024 px"),
        ("not_in_dnd", "nothing to end"),
        ("file_deleted", "was deleted"),
    ],
)
def test_s2_hints(code, text):
    assert text in describe_error(_error(code))


def test_failure_rounds_retry_after_up():
    result = failure(_error("ratelimited", status=429, retry_after=1.2))
    assert (result.error_code, result.retry_after) == ("rate_limited", 2.0)
    assert result.data == {"slack_error": "ratelimited", "status": 429}


def test_messages_carry_no_arguments_or_token():
    sentinel = "SENTINEL-ARG-VALUE-42"
    for reply in (
        slack_error("channel_not_found"),
        slack_error("invalid_auth"),
        response(500, content=sentinel.encode()),
        response(200, content=b"not json " + sentinel.encode()),
    ):
        h = harness(reply)
        result = h.call("SLACK_SEND_MESSAGE", {"channel": "C0123456", "markdown_text": sentinel})
        assert result.successful is False
        assert sentinel not in result.error
        assert USER_TOKEN not in result.error
