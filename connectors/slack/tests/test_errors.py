"""Slack failures -> canonical error codes and messages (plan §4.4): table
driven over every code, then the transport/HTTP order, the hints, and what a
message must never carry."""

from __future__ import annotations

import pytest

from aisquare_pipe_slack.rest import SlackAPIError
from aisquare_pipe_slack.toolkit import describe_error, error_code, failure

from .fakes import USER_TOKEN, harness, response, slack_error

TABLE = {
    "unauthorized": ["not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired"],
    "forbidden": [
        "missing_scope", "no_permission", "not_allowed_token_type", "restricted_action", "not_authorized",
        "ekm_access_denied", "team_access_not_granted", "enterprise_is_restricted", "accesslimited",
        "two_factor_setup_required", "cant_update_message", "cant_delete_message", "not_in_channel",
        "plan_upgrade_required", "paid_only", "permission_denied",
    ],
    "not_found": [
        "channel_not_found", "user_not_found", "users_not_found", "message_not_found", "thread_not_found",
        "file_not_found", "canvas_not_found", "bookmark_not_found", "no_such_subteam", "team_not_found",
        "not_found",
    ],
    "conflict": [
        "already_reacted", "no_reaction", "already_pinned", "not_pinned", "name_taken", "is_archived",
        "already_archived", "not_archived", "already_in_channel", "channel_canvas_already_exists",
        "cant_invite_self",
    ],
    "rate_limited": ["ratelimited", "rate_limited", "restricted_too_many"],
    "invalid_arguments": [
        "invalid_arguments", "invalid_arg_name", "invalid_cursor", "invalid_ts_latest", "invalid_ts_oldest",
        "msg_too_long", "msg_blocks_too_long", "no_text", "invalid_blocks", "invalid_name",
        "invalid_name_punctuation", "time_in_past", "time_too_far", "too_many_users", "user_not_in_channel",
        "invalid_user", "snippet_too_large", "invalid_charset", "invalid_form_data", "invalid_post_type",
        "missing_post_type", "markdown_text_conflict",
    ],
    "upstream_error": [
        "fatal_error", "internal_error", "service_unavailable", "request_timeout", "something_new_from_slack",
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
        (_error("not_in_channel"), "join the channel first with SLACK_JOIN_AN_EXISTING_CONVERSATION"),
        (_error("already_reacted"), "already in that state; no change needed"),
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
