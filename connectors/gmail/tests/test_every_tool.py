"""The failure contract, tool by tool: every tool in the catalog is run
against a successful answer, a 401, a missing-scope 403, a 429 and a host
time budget, and is refused before any request when its arguments are bad.

``CASES`` holds one valid call per tool with the answers Gmail would give;
``test_cases_cover_the_catalog`` keeps it in step with the catalog."""

from __future__ import annotations

import pytest
from aisquare.pipe import ERROR_CODES

from aisquare_pipe_gmail import REQUIRED_SCOPES
from aisquare_pipe_gmail.toolkit import ERROR_INSUFFICIENT_SCOPE
from aisquare_pipe_gmail.tools import CATALOG

from .fakes import (
    CONFIG,
    TOKEN,
    harness,
    listing,
    message,
    rate_limited,
    response,
    scope_denied,
    unauthorized,
)

THREAD = {"id": "t1", "historyId": "7", "messages": [message("m1", thread_id="t1")]}
SENT = {"id": "m9", "threadId": "t1", "labelIds": ["SENT"]}

# slug -> (arguments, answers for a successful call, (method, path) of the first request)
CASES: dict[str, tuple[dict, list, tuple[str, str]]] = {
    "GMAIL_FETCH_EMAILS": (
        {"query": "is:unread", "max_results": 1},
        [response(200, listing(["m1"])), response(200, message("m1"))],
        ("GET", "/me/messages"),
    ),
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID": (
        {"message_id": "m1"},
        [response(200, message("m1"))],
        ("GET", "/me/messages/m1"),
    ),
    "GMAIL_LIST_THREADS": (
        {"max_results": 5},
        [response(200, {"threads": [{"id": "t1", "snippet": "hi", "historyId": "7"}]})],
        ("GET", "/me/threads"),
    ),
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": (
        {"thread_id": "t1"},
        [response(200, THREAD)],
        ("GET", "/me/threads/t1"),
    ),
    "GMAIL_GET_ATTACHMENT": (
        {"message_id": "m1", "attachment_id": "ANGjdJ8", "file_name": "a.txt"},
        [response(200, {"size": 2, "data": "aGk"})],
        ("GET", "/me/messages/m1/attachments/ANGjdJ8"),
    ),
    "GMAIL_LIST_LABELS": (
        {},
        [response(200, {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]})],
        ("GET", "/me/labels"),
    ),
    "GMAIL_GET_PROFILE": (
        {},
        [response(200, {"emailAddress": "me@example.com", "messagesTotal": 3})],
        ("GET", "/me/profile"),
    ),
    "GMAIL_CREATE_EMAIL_DRAFT": (
        {"recipient_email": "ann@example.com", "subject": "Hi", "body": "Draft"},
        [response(200, {"id": "r-1", "message": {"id": "m2", "threadId": "t2", "labelIds": ["DRAFT"]}})],
        ("POST", "/me/drafts"),
    ),
    "GMAIL_SEND_EMAIL": (
        {"recipient_email": "ann@example.com", "subject": "Hi", "body": "Sent"},
        [response(200, {"id": "m3", "threadId": "t3", "labelIds": ["SENT"]})],
        ("POST", "/me/messages/send"),
    ),
    "GMAIL_REPLY_TO_THREAD": (
        {"thread_id": "t1", "message_body": "Thanks!"},
        [response(200, THREAD), response(200, SENT)],
        ("GET", "/me/threads/t1"),
    ),
    "GMAIL_ADD_LABEL_TO_EMAIL": (
        {"message_id": "m1", "remove_label_ids": ["UNREAD"]},
        [response(200, {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"]})],
        ("POST", "/me/messages/m1/modify"),
    ),
    "GMAIL_MODIFY_THREAD_LABELS": (
        {"thread_id": "t1", "remove_label_ids": ["INBOX"]},
        [response(200, {"id": "t1", "messages": [{"id": "m1", "labelIds": ["UNREAD"]}]})],
        ("POST", "/me/threads/t1/modify"),
    ),
    "GMAIL_MOVE_TO_TRASH": (
        {"message_id": "m1"},
        [response(200, {"id": "m1", "threadId": "t1", "labelIds": ["TRASH"]})],
        ("POST", "/me/messages/m1/trash"),
    ),
}

SLUGS = sorted(CASES)

# slug -> bad argument sets (each refused as invalid_arguments, no request sent)
BAD_ARGUMENTS: dict[str, list[dict]] = {
    "GMAIL_FETCH_EMAILS": [{"max_results": "ten"}, {"verbose": "yes"}, {"user_id": "../admin"}, {"label_ids": "[not json"}],
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID": [{}, {"message_id": ".."}, {"message_id": "m1", "format": "eml"}],
    "GMAIL_LIST_THREADS": [{"max_results": 0.5}, {"page_token": 7}],
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": [{}, {"thread_id": "   "}, {"thread_id": 12}],
    "GMAIL_GET_ATTACHMENT": [{"message_id": "m1"}, {"message_id": "m1", "attachment_id": "."}, {"attachment_id": "a"}],
    "GMAIL_LIST_LABELS": [{"include_details": "true"}, {"user_id": "not an address"}],
    "GMAIL_GET_PROFILE": [{"user_id": ["me"]}],
    "GMAIL_CREATE_EMAIL_DRAFT": [
        {},
        {"recipient_email": "ann@example.com"},
        {"subject": "x", "recipient_email": "not-an-address"},
        {"subject": "x\r\nBcc: evil@example.com"},
        {"subject": "x", "attachment": {"name": "a.txt", "file_content_bytes": "%%%"}},
    ],
    "GMAIL_SEND_EMAIL": [
        {"subject": "no recipient", "body": "x"},
        {"recipient_email": "ann@example.com"},
        {"recipient_email": "ann@example.com", "body": "x", "cc": ["bad address"]},
        {"recipient_email": "ann@example.com", "body": "x", "from_email": "a@x.co, b@y.co"},
        {"recipient_email": "ann@example.com", "body": "x", "attachment": {"name": "a", "s3key": "k"}},
        {"recipient_email": "ann@example.com\nBcc: evil@example.com", "body": "x"},
    ],
    "GMAIL_REPLY_TO_THREAD": [{"message_body": "x"}, {"thread_id": "t1"}, {"thread_id": "..", "message_body": "x"}],
    "GMAIL_ADD_LABEL_TO_EMAIL": [{"message_id": "m1"}, {"add_label_ids": ["STARRED"]}, {"message_id": "m1", "add_label_ids": [3]}],
    "GMAIL_MODIFY_THREAD_LABELS": [{"thread_id": "t1"}, {"thread_id": "t1", "add_label_ids": []}],
    "GMAIL_MOVE_TO_TRASH": [{}, {"message_id": ""}, {"message_id": "."}],
}


def test_cases_cover_the_catalog():
    slugs = {tool.spec.slug for tool in CATALOG}
    assert set(CASES) == slugs
    assert set(BAD_ARGUMENTS) == slugs


@pytest.mark.parametrize("slug", SLUGS)
def test_succeeds(slug):
    args, answers, (method, path) = CASES[slug]
    h = harness(*answers)
    result = h.call(slug, args)
    assert result.successful, result.error
    assert (h.session.calls[0].method, h.session.calls[0].path) == (method, path)
    assert h.session.responses == []
    for call in h.session.calls:
        assert call.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in call.url


@pytest.mark.parametrize("slug", SLUGS)
def test_401_is_unauthorized(slug):
    args, _, _ = CASES[slug]
    h = harness(unauthorized())
    result = h.call(slug, args)
    assert (result.successful, result.error_code, result.retry_after) == (False, "unauthorized", None)
    assert "reconnect" in result.error
    assert len(h.session.calls) == 1


@pytest.mark.parametrize("slug", SLUGS)
def test_scope_403_is_insufficient_scope(slug):
    args, _, _ = CASES[slug]
    h = harness(scope_denied())
    result = h.call(slug, args)
    assert result.error_code == ERROR_INSUFFICIENT_SCOPE
    assert result.error_code in ERROR_CODES
    assert result.data == {
        "status": 403,
        "reason": "insufficientPermissions",
        "insufficient_scope": True,
        "required_scopes": list(REQUIRED_SCOPES[slug]),
    }
    short = REQUIRED_SCOPES[slug][-1].rsplit("/", 1)[-1]
    assert short in result.error
    assert len(h.session.calls) == 1  # not retried


@pytest.mark.parametrize("slug", SLUGS)
def test_429_is_rate_limited_with_retry_after(slug):
    args, _, _ = CASES[slug]
    h = harness(*rate_limited(1, retry_after="120"))
    result = h.call(slug, args)
    assert (result.error_code, result.retry_after) == ("rate_limited", 120.0)
    assert h.sleeps == []  # 120s is over the 60s retry-wait cap: fail now


@pytest.mark.parametrize("slug", SLUGS)
def test_429_is_retried_then_succeeds(slug):
    args, answers, _ = CASES[slug]
    h = harness(*rate_limited(1), *answers)
    result = h.call(slug, args)
    assert result.successful, result.error
    assert h.sleeps == [1.0]


@pytest.mark.parametrize("slug", SLUGS)
def test_budget_caps_every_request(slug):
    args, answers, _ = CASES[slug]
    h = harness(*answers)
    result = h.call(slug, args, config={**CONFIG, "timeout_seconds": 5})
    assert result.successful, result.error
    assert all(0 < call.timeout <= 5 for call in h.session.calls)


@pytest.mark.parametrize("slug", SLUGS)
def test_a_retry_that_would_outlast_the_budget_fails_now(slug):
    args, _, _ = CASES[slug]
    h = harness(*rate_limited(1, retry_after="3"))
    result = h.call(slug, args, config={**CONFIG, "timeout_seconds": 4})
    assert (result.error_code, result.retry_after) == ("rate_limited", 3.0)
    assert h.sleeps == []
    assert len(h.session.calls) == 1


@pytest.mark.parametrize(
    ("slug", "args"),
    [(slug, args) for slug in SLUGS for args in BAD_ARGUMENTS[slug]],
    ids=[f"{slug.removeprefix('GMAIL_')}-{i}" for slug in SLUGS for i, _ in enumerate(BAD_ARGUMENTS[slug])],
)
def test_bad_arguments_are_refused_before_any_request(slug, args):
    h = harness()
    result = h.call(slug, args)
    assert result.successful is False
    assert result.error_code == "invalid_arguments", result.error
    assert h.session.calls == []


@pytest.mark.parametrize("slug", SLUGS)
def test_no_credentials_means_no_request(slug):
    args, _, _ = CASES[slug]
    h = harness()
    assert h.call(slug, args, config={}).error_code == "unauthorized"
    assert h.session.calls == []
