"""The catalog: identity, Composio's slugs and argument names, read/write
and destructive flags, least-privilege scopes, argument aliases and which
tools a model finds for common asks."""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from aisquare_pipe_gmail import GMAIL_OAUTH, REQUIRED_SCOPES, GmailToolkit
from aisquare_pipe_gmail.tools import (
    COMPOSE_SCOPE,
    MODIFY_SCOPE,
    READONLY_SCOPE,
    SEND_SCOPE,
    TOOLS,
    apply_aliases,
)

from .fakes import harness, listing, message, response

#: Composio's Gmail slugs this toolkit mirrors, with the arguments Composio
#: documents for each (docs.composio.dev/toolkits/gmail, read 2026-10-30).
COMPOSIO_ARGUMENTS = {
    "GMAIL_FETCH_EMAILS": {"query", "label_ids", "max_results", "page_token", "include_payload", "include_spam_trash", "verbose", "ids_only", "user_id"},
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID": {"message_id", "format", "user_id"},
    "GMAIL_LIST_THREADS": {"query", "max_results", "page_token", "verbose", "user_id"},
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": {"thread_id", "user_id"},
    "GMAIL_GET_ATTACHMENT": {"message_id", "attachment_id", "file_name", "user_id"},
    "GMAIL_LIST_LABELS": {"include_details", "user_id"},
    "GMAIL_GET_PROFILE": {"user_id"},
    "GMAIL_CREATE_EMAIL_DRAFT": {"recipient_email", "extra_recipients", "cc", "bcc", "subject", "body", "is_html", "thread_id", "attachment", "user_id"},
    "GMAIL_SEND_EMAIL": {"recipient_email", "extra_recipients", "cc", "bcc", "subject", "body", "is_html", "attachment", "from_email", "user_id"},
    "GMAIL_REPLY_TO_THREAD": {"thread_id", "message_body", "recipient_email", "extra_recipients", "cc", "bcc", "is_html", "attachment", "user_id"},
    "GMAIL_ADD_LABEL_TO_EMAIL": {"message_id", "add_label_ids", "remove_label_ids", "user_id"},
    "GMAIL_MODIFY_THREAD_LABELS": {"thread_id", "add_label_ids", "remove_label_ids", "user_id"},
    "GMAIL_MOVE_TO_TRASH": {"message_id", "user_id"},
}
#: Required arguments, as Composio marks them (GET_ATTACHMENT's file_name is
#: optional here: the file is named "attachment" without it).
REQUIRED = {
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID": ["message_id"],
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": ["thread_id"],
    "GMAIL_GET_ATTACHMENT": ["message_id", "attachment_id"],
    "GMAIL_REPLY_TO_THREAD": ["thread_id"],
    "GMAIL_ADD_LABEL_TO_EMAIL": ["message_id"],
    "GMAIL_MODIFY_THREAD_LABELS": ["thread_id"],
    "GMAIL_MOVE_TO_TRASH": ["message_id"],
}


class TestCatalog:
    def test_registered_as_the_gmail_toolkit(self):
        (ep,) = [ep for ep in entry_points(group="aisquare_pipe.toolkits") if ep.name == "gmail"]
        assert ep.load() is GmailToolkit

    def test_identity(self):
        toolkit = GmailToolkit()
        assert (toolkit.name, toolkit.slug_prefix, toolkit.display_name) == ("gmail", "GMAIL_", "Gmail")
        assert toolkit.oauth is GMAIL_OAUTH

    def test_composio_slugs_and_arguments(self):
        tools = {t.slug: t for t in GmailToolkit().tools()}
        assert set(tools) == set(COMPOSIO_ARGUMENTS)
        for slug, names in COMPOSIO_ARGUMENTS.items():
            assert set(tools[slug].input_schema["properties"]) == names, slug
            assert tools[slug].input_schema["required"] == REQUIRED.get(slug, []), slug

    def test_read_only_and_destructive_flags(self):
        flags = {t.slug: (t.read_only, t.destructive) for t in GmailToolkit().tools()}
        reads = {s for s, (ro, _) in flags.items() if ro}
        assert reads == {
            "GMAIL_FETCH_EMAILS",
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            "GMAIL_LIST_THREADS",
            "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
            "GMAIL_GET_ATTACHMENT",
            "GMAIL_LIST_LABELS",
            "GMAIL_GET_PROFILE",
        }
        assert {s for s, (_, d) in flags.items() if d} == {"GMAIL_MOVE_TO_TRASH"}

    def test_descriptions_fit_and_name_their_scope(self):
        for tool in GmailToolkit().tools():
            assert len(tool.description) <= 1024
            for scope in REQUIRED_SCOPES[tool.slug]:
                assert scope.rsplit("/", 1)[-1] in tool.description, tool.slug


class TestScopes:
    def test_default_consent_is_read_only(self):
        assert GMAIL_OAUTH.default_scopes == (READONLY_SCOPE,)

    def test_reads_need_only_the_default(self):
        for tool in GmailToolkit().tools():
            if tool.read_only:
                assert REQUIRED_SCOPES[tool.slug] == (READONLY_SCOPE,), tool.slug

    def test_writes_name_their_extra_scope(self):
        assert REQUIRED_SCOPES["GMAIL_CREATE_EMAIL_DRAFT"] == (COMPOSE_SCOPE,)
        assert REQUIRED_SCOPES["GMAIL_SEND_EMAIL"] == (SEND_SCOPE,)
        assert REQUIRED_SCOPES["GMAIL_REPLY_TO_THREAD"] == (READONLY_SCOPE, SEND_SCOPE)
        for slug in ("GMAIL_ADD_LABEL_TO_EMAIL", "GMAIL_MODIFY_THREAD_LABELS", "GMAIL_MOVE_TO_TRASH"):
            assert REQUIRED_SCOPES[slug] == (MODIFY_SCOPE,)

    def test_no_tool_needs_full_mailbox_access(self):
        assert all("mail.google.com" not in s for scopes in REQUIRED_SCOPES.values() for s in scopes)

    def test_tool_specs_carry_their_scopes_and_risk(self):
        for tool in GmailToolkit().tools():
            assert tool.required_scopes == REQUIRED_SCOPES[tool.slug]
            assert tool.risk == ("destructive" if tool.destructive else "read" if tool.read_only else "write")

    @pytest.mark.parametrize(
        ("granted", "writes"),
        [
            ([READONLY_SCOPE], set()),
            ([READONLY_SCOPE, SEND_SCOPE], {"GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD"}),
            ([READONLY_SCOPE, COMPOSE_SCOPE], {"GMAIL_CREATE_EMAIL_DRAFT", "GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD"}),
            ([MODIFY_SCOPE], {"GMAIL_CREATE_EMAIL_DRAFT", "GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD", "GMAIL_ADD_LABEL_TO_EMAIL", "GMAIL_MODIFY_THREAD_LABELS", "GMAIL_MOVE_TO_TRASH"}),
            (["https://mail.google.com/"], {"GMAIL_CREATE_EMAIL_DRAFT", "GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD", "GMAIL_ADD_LABEL_TO_EMAIL", "GMAIL_MODIFY_THREAD_LABELS", "GMAIL_MOVE_TO_TRASH"}),
        ],
    )
    def test_granted_scopes_decide_what_is_listed(self, granted, writes):
        """``gmail.modify`` covers read, compose and send; ``gmail.compose``
        covers send; ``https://mail.google.com/`` covers everything."""
        listed = {t.slug for t in GmailToolkit().available_tools({"access_token": "t", "granted_scopes": granted})}
        reads = {t.slug for t in GmailToolkit().tools() if t.read_only}
        assert listed == reads | writes

    def test_send_only_connection_lists_only_send(self):
        listed = [t.slug for t in GmailToolkit().available_tools({"granted_scopes": SEND_SCOPE})]
        assert listed == ["GMAIL_SEND_EMAIL"]

    def test_unknown_grants_filter_nothing(self):
        assert len(GmailToolkit().available_tools({"access_token": "t"})) == 13

    def test_a_write_without_its_scope_is_refused_before_any_request(self):
        h = harness()
        result = h.call(
            "GMAIL_SEND_EMAIL",
            {"recipient_email": "a@x.co", "body": "x"},
            config={"access_token": "t", "granted_scopes": [READONLY_SCOPE]},
        )
        assert result.error_code == "insufficient_scope"
        assert h.session.calls == []

    def test_a_covered_write_runs(self):
        h = harness(response(200, {"id": "m1", "threadId": "t1", "labelIds": ["TRASH"]}))
        result = h.call("GMAIL_MOVE_TO_TRASH", {"message_id": "m1"}, config={"access_token": "t", "granted_scopes": ["https://mail.google.com/"]})
        assert result.successful, result.error


class TestAliases:
    def test_camel_case_ids(self):
        h = harness(response(200, message("m1")))
        assert h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"messageId": "m1"}).successful

    def test_composio_aliases(self):
        spec = TOOLS["GMAIL_SEND_EMAIL"]
        assert apply_aliases(spec.spec, {"to": "a@x.co", "message_body": "x"}, spec.aliases) == {
            "recipient_email": "a@x.co",
            "body": "x",
        }
        reply = TOOLS["GMAIL_REPLY_TO_THREAD"]
        assert apply_aliases(reply.spec, {"threadId": "t", "body": "x"}, reply.aliases) == {"thread_id": "t", "message_body": "x"}

    def test_the_schema_spelling_wins(self):
        spec = TOOLS["GMAIL_SEND_EMAIL"]
        args = apply_aliases(spec.spec, {"recipient_email": "a@x.co", "to": "b@x.co"}, spec.aliases)
        assert args == {"recipient_email": "a@x.co", "to": "b@x.co"}

    def test_lowercase_slug_still_gets_aliases(self):
        h = harness(response(200, message("m1")))
        assert h.call("gmail_fetch_message_by_message_id", {"messageId": "m1"}).successful

    def test_non_mapping_arguments_fail_validation(self):
        result = GmailToolkit().call_tool("GMAIL_GET_PROFILE", ["me"], {"access_token": "t"})  # type: ignore[arg-type]
        assert result.error_code == "invalid_arguments"

    def test_unknown_slug(self):
        assert harness().call("GMAIL_DELETE_EVERYTHING", {}).error_code == "unknown_tool"


class TestToolSelection:
    @pytest.mark.parametrize(
        ("query", "slug"),
        [
            ("show my unread emails", "GMAIL_FETCH_EMAILS"),
            ("search my inbox for invoices from Acme", "GMAIL_FETCH_EMAILS"),
            ("send an email to ann about the launch", "GMAIL_SEND_EMAIL"),
            ("reply to that conversation", "GMAIL_REPLY_TO_THREAD"),
            ("draft an email to the team", "GMAIL_CREATE_EMAIL_DRAFT"),
            ("download the pdf attachment", "GMAIL_GET_ATTACHMENT"),
            ("mark this email as read", "GMAIL_ADD_LABEL_TO_EMAIL"),
            ("trash the email from Bob", "GMAIL_MOVE_TO_TRASH"),
            ("what labels do I have", "GMAIL_LIST_LABELS"),
            ("what is my email address", "GMAIL_GET_PROFILE"),
        ],
    )
    def test_in_top_three(self, query, slug):
        assert slug in [t.slug for t in GmailToolkit().search_tools(query, limit=3)]


def test_list_then_read_round_trip():
    h = harness(response(200, listing(["m1"])), response(200, message("m1")), response(200, message("m1")))
    found = h.call("GMAIL_FETCH_EMAILS", {"query": "Q3"}).data["items"][0]
    again = h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": found["messageId"]}).data
    assert again["messageId"] == found["messageId"]
