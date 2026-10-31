"""``REQUIRED_SCOPES`` against what each tool really sends: every request a
tool can make (all its paths: label-name lookups, a draft's thread read) must
be allowed by the scopes it declares, as Google documents per method, and
each declared scope must be needed by one of them. Core's ``granted_scopes``
pre-flight trusts these lists, so an understated one fails at Google and an
overstated one hides a tool that would have worked."""

from __future__ import annotations

import functools
import re

import pytest

from aisquare_pipe_gmail import REQUIRED_SCOPES, GmailToolkit
from aisquare_pipe_gmail.tools import (
    COMPOSE_SCOPE,
    FULL_SCOPE,
    MODIFY_SCOPE,
    READONLY_SCOPE,
    SEND_SCOPE,
)

from .fakes import CONFIG, harness, listing, message, response
from .test_every_tool import CASES

#: Scopes Google accepts per Gmail method (developers.google.com/gmail/api/reference/rest),
#: limited to the ones this toolkit deals in. ``(method, path regex, scopes)``.
ENDPOINTS: list[tuple[str, str, frozenset[str]]] = [
    ("GET", r"/[^/]+/messages", frozenset({FULL_SCOPE, MODIFY_SCOPE, READONLY_SCOPE})),
    ("GET", r"/[^/]+/messages/[^/]+", frozenset({FULL_SCOPE, MODIFY_SCOPE, READONLY_SCOPE})),
    ("GET", r"/[^/]+/messages/[^/]+/attachments/[^/]+", frozenset({FULL_SCOPE, MODIFY_SCOPE, READONLY_SCOPE})),
    ("GET", r"/[^/]+/threads(/[^/]+)?", frozenset({FULL_SCOPE, MODIFY_SCOPE, READONLY_SCOPE})),
    ("GET", r"/[^/]+/labels(/[^/]+)?", frozenset({FULL_SCOPE, MODIFY_SCOPE, READONLY_SCOPE})),
    ("GET", r"/[^/]+/profile", frozenset({FULL_SCOPE, MODIFY_SCOPE, READONLY_SCOPE, COMPOSE_SCOPE})),
    ("POST", r"/[^/]+/drafts", frozenset({FULL_SCOPE, MODIFY_SCOPE, COMPOSE_SCOPE})),
    ("POST", r"/[^/]+/messages/send", frozenset({FULL_SCOPE, MODIFY_SCOPE, COMPOSE_SCOPE, SEND_SCOPE})),
    ("POST", r"/[^/]+/(messages|threads)/[^/]+/modify", frozenset({FULL_SCOPE, MODIFY_SCOPE})),
    ("POST", r"/[^/]+/messages/[^/]+/trash", frozenset({FULL_SCOPE, MODIFY_SCOPE})),
]

LABELS = {"labels": [{"id": "Label_7", "name": "Receipts"}]}
THREAD = {"id": "t1", "messages": [message("m1", thread_id="t1", extra_headers={"Message_ID": "<a@b>"})]}

#: Paths the matrix in test_every_tool.py does not take.
EXTRA: list[tuple[str, dict, list]] = [
    ("GMAIL_FETCH_EMAILS", {"label_ids": ["Receipts"]}, [response(200, LABELS), response(200, listing(["m1"])), response(200, message("m1"))]),
    ("GMAIL_FETCH_EMAILS", {"verbose": False}, [response(200, listing(["m1"])), response(200, message("m1"))]),
    ("GMAIL_LIST_THREADS", {"verbose": True}, [response(200, {"threads": [{"id": "t1"}]}), response(200, THREAD)]),
    ("GMAIL_LIST_LABELS", {"include_details": True}, [response(200, LABELS), response(200, LABELS["labels"][0])]),
    ("GMAIL_CREATE_EMAIL_DRAFT", {"thread_id": "t1", "body": "x"}, [response(200, THREAD), response(200, {"id": "r1"})]),
    ("GMAIL_ADD_LABEL_TO_EMAIL", {"message_id": "m1", "add_label_ids": ["Receipts"]}, [response(200, LABELS), response(200, {"id": "m1"})]),
    ("GMAIL_MODIFY_THREAD_LABELS", {"thread_id": "t1", "remove_label_ids": ["Receipts"]}, [response(200, LABELS), response(200, {"id": "t1"})]),
]

SCENARIOS = [(slug, args, answers) for slug, (args, answers, _) in CASES.items()] + EXTRA


def _endpoint(method: str, path: str) -> frozenset[str]:
    for verb, pattern, scopes in ENDPOINTS:
        if verb == method and re.fullmatch(pattern, path):
            return scopes
    raise AssertionError(f"no scope entry for {method} {path}: add the endpoint to ENDPOINTS")


@functools.cache
def _requests_by_tool() -> dict[str, list[frozenset[str]]]:
    """Each tool's requests (as the scopes their endpoints accept), over every
    scenario. Run once, inside the first test (so under the socket guard)."""
    seen: dict[str, list[frozenset[str]]] = {}
    for slug, args, answers in SCENARIOS:
        h = harness(*answers)
        result = h.call(slug, args, config={**CONFIG, "granted_scopes": list(REQUIRED_SCOPES[slug])})
        assert result.successful, f"{slug}: {result.error}"
        seen.setdefault(slug, []).extend(_endpoint(c.method, c.path) for c in h.session.calls)
    return seen


def _allowed(accepted: frozenset[str], granted: frozenset[str]) -> bool:
    return any(GmailToolkit().scope_granted(scope, granted) for scope in accepted)


def test_every_tool_is_audited():
    assert set(_requests_by_tool()) == set(REQUIRED_SCOPES)


@pytest.mark.parametrize("slug", sorted(REQUIRED_SCOPES))
def test_declared_scopes_allow_every_request(slug):
    granted = frozenset(REQUIRED_SCOPES[slug])
    for accepted in _requests_by_tool()[slug]:
        assert _allowed(accepted, granted), f"{slug} sends a request its scopes do not allow ({sorted(accepted)})"


@pytest.mark.parametrize("slug", sorted(REQUIRED_SCOPES))
def test_every_declared_scope_is_needed(slug):
    for scope in REQUIRED_SCOPES[slug]:
        fewer = frozenset(REQUIRED_SCOPES[slug]) - {scope}
        assert any(not _allowed(accepted, fewer) for accepted in _requests_by_tool()[slug]), f"{slug} does not need {scope}"


class TestDraftInAThread:
    """The corrected tool: a draft with thread_id reads the thread first."""

    def test_compose_alone_does_not_list_or_run_it(self):
        config = {**CONFIG, "granted_scopes": [COMPOSE_SCOPE]}
        assert "GMAIL_CREATE_EMAIL_DRAFT" not in {t.slug for t in GmailToolkit().available_tools(config)}
        h = harness()
        result = h.call("GMAIL_CREATE_EMAIL_DRAFT", {"thread_id": "t1", "body": "x"}, config=config)
        assert result.error_code == "insufficient_scope"
        assert h.session.calls == []

    @pytest.mark.parametrize("granted", [[READONLY_SCOPE, COMPOSE_SCOPE], [MODIFY_SCOPE], [FULL_SCOPE]])
    def test_read_plus_compose_or_a_broader_scope_runs_it(self, granted):
        h = harness(response(200, THREAD), response(200, {"id": "r1"}))
        result = h.call("GMAIL_CREATE_EMAIL_DRAFT", {"thread_id": "t1", "body": "x"}, config={**CONFIG, "granted_scopes": granted})
        assert result.successful, result.error
        assert [c.path for c in h.session.calls] == ["/me/threads/t1", "/me/drafts"]

    def test_compose_still_covers_sending(self):
        listed = {t.slug for t in GmailToolkit().available_tools({**CONFIG, "granted_scopes": [COMPOSE_SCOPE]})}
        assert listed == {"GMAIL_SEND_EMAIL"}
