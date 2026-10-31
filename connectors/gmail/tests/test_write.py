"""Write tools: drafts, sends, replies threaded into their conversation,
label changes (mark read, archive, user labels by name) and trash."""

from __future__ import annotations

import email
import email.policy

from .fakes import google_error, harness, message, raw_message, response

SENT = {"id": "m9", "threadId": "t1", "labelIds": ["SENT"]}


def _thread(*messages: dict) -> dict:
    return {"id": "t1", "historyId": "5", "messages": list(messages)}


def _parsed(call) -> email.message.EmailMessage:
    parsed = email.message_from_bytes(raw_message(call), policy=email.policy.default)
    assert isinstance(parsed, email.message.EmailMessage)
    return parsed


INCOMING = message(
    "m1",
    thread_id="t1",
    subject="Q3 plan",
    sender="Ann <ann@example.com>",
    extra_headers={"Message_ID": "<CAF1@mail.example.com>", "References": "<CAF0@mail.example.com>"},
)


class TestSend:
    def test_send_returns_gmails_resource(self):
        h = harness(response(200, SENT))
        result = h.call("GMAIL_SEND_EMAIL", {"recipient_email": "ann@example.com", "subject": "Hi", "body": "Hello"})
        assert result.data == SENT
        assert (h.session.last.method, h.session.last.path) == ("POST", "/me/messages/send")

    def test_send_is_not_retried_on_a_5xx(self):
        h = harness(google_error(503, "Backend Error", "backendError"))
        result = h.call("GMAIL_SEND_EMAIL", {"recipient_email": "ann@example.com", "body": "x"})
        assert result.error_code == "upstream_error"
        assert len(h.session.calls) == 1  # a half-done send must not go out twice

    def test_bcc_only_is_enough(self):
        h = harness(response(200, SENT))
        assert h.call("GMAIL_SEND_EMAIL", {"bcc": ["a@x.co"], "subject": "x"}).successful

    def test_attachment_only_is_enough(self):
        h = harness(response(200, SENT))
        assert h.call("GMAIL_SEND_EMAIL", {"to": "a@x.co", "attachment": {"name": "a", "file_content_bytes": "eA=="}}).successful


class TestDraft:
    def test_draft(self):
        created = {"id": "r-55", "message": {"id": "m2", "threadId": "t2", "labelIds": ["DRAFT"]}}
        h = harness(response(200, created))
        result = h.call("GMAIL_CREATE_EMAIL_DRAFT", {"recipient_email": "a@x.co", "subject": "Later", "body": "draft"})
        assert result.data == created
        call = h.session.last
        assert call.path == "/me/drafts" and set(call.json) == {"message"} and set(call.json["message"]) == {"raw"}
        assert _parsed(call)["Subject"] == "Later"

    def test_draft_without_recipients(self):
        h = harness(response(200, {"id": "r1"}))
        assert h.call("GMAIL_CREATE_EMAIL_DRAFT", {"subject": "Idea"}).successful
        assert _parsed(h.session.last)["To"] is None

    def test_draft_reply_in_a_thread(self):
        h = harness(response(200, _thread(INCOMING)), response(200, {"id": "r1", "message": {"id": "m2", "threadId": "t1"}}))
        h.call("GMAIL_CREATE_EMAIL_DRAFT", {"thread_id": "t1", "body": "Draft answer"})
        assert h.session.calls[0].params == {
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References"],
        }
        call = h.session.last
        assert call.json["message"]["threadId"] == "t1"
        msg = _parsed(call)
        assert (msg["Subject"], msg["To"], msg["In-Reply-To"]) == ("Re: Q3 plan", "Ann <ann@example.com>", "<CAF1@mail.example.com>")
        assert msg["References"] == "<CAF0@mail.example.com> <CAF1@mail.example.com>"


class TestReply:
    def test_reply_goes_to_the_last_sender_in_the_thread(self):
        h = harness(response(200, _thread(INCOMING)), response(200, SENT))
        result = h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "Sounds good"})
        assert result.data == SENT
        call = h.session.last
        assert (call.path, call.json["threadId"]) == ("/me/messages/send", "t1")
        msg = _parsed(call)
        assert msg["To"] == "Ann <ann@example.com>"
        assert msg["Subject"] == "Re: Q3 plan"
        assert msg["In-Reply-To"] == "<CAF1@mail.example.com>"
        assert msg["References"] == "<CAF0@mail.example.com> <CAF1@mail.example.com>"
        assert "Sounds good" in msg.get_content()

    def test_reply_to_header_wins_over_from(self):
        incoming = message("m1", thread_id="t1", extra_headers={"Reply_To": "Support <help@example.com>", "Message_ID": "<x@y>"})
        h = harness(response(200, _thread(incoming)), response(200, SENT))
        h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "body": "On it"})
        assert _parsed(h.session.last)["To"] == "Support <help@example.com>"

    def test_follow_up_on_own_message_goes_to_its_recipients(self):
        own = message("m2", thread_id="t1", sender="Me <me@example.com>", to="Bob <bob@example.com>", labels=("SENT",),
                      extra_headers={"Message_ID": "<own@x>"})
        h = harness(response(200, _thread(INCOMING, own)), response(200, SENT))
        h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "Ping"})
        msg = _parsed(h.session.last)
        assert (msg["To"], msg["In-Reply-To"]) == ("Bob <bob@example.com>", "<own@x>")

    def test_drafts_in_the_thread_are_ignored(self):
        draft = message("m3", thread_id="t1", sender="Me <me@example.com>", labels=("DRAFT",))
        h = harness(response(200, _thread(INCOMING, draft)), response(200, SENT))
        h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "x"})
        assert _parsed(h.session.last)["To"] == "Ann <ann@example.com>"

    def test_explicit_recipients_win(self):
        h = harness(response(200, _thread(INCOMING)), response(200, SENT))
        h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "x", "recipient_email": "cy@x.co", "cc": ["dee@x.co"]})
        msg = _parsed(h.session.last)
        assert (msg["To"], msg["Cc"]) == ("cy@x.co", "dee@x.co")

    def test_html_reply(self):
        h = harness(response(200, _thread(INCOMING)), response(200, SENT))
        h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "<p>Yes</p>", "is_html": True})
        assert _parsed(h.session.last).get_content_type() == "multipart/alternative"

    def test_empty_thread(self):
        h = harness(response(200, {"id": "t1", "messages": []}))
        result = h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "x"})
        assert result.error_code == "invalid_arguments"
        assert len(h.session.calls) == 1

    def test_thread_with_no_usable_sender(self):
        incoming = message("m1", thread_id="t1", sender="undisclosed-recipients:;")
        h = harness(response(200, _thread(incoming)))
        result = h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "x"})
        assert result.error_code == "invalid_arguments"
        assert "recipient_email" in result.error

    def test_missing_thread_is_not_found(self):
        h = harness(google_error(404, "Requested entity was not found.", "notFound"))
        assert h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t404", "message_body": "x"}).error_code == "not_found"

    def test_folded_headers_are_unfolded(self):
        incoming = message("m1", thread_id="t1", subject="Long\r\n subject", extra_headers={"Message_ID": "<a@b>", "References": "<x@y>\r\n <z@w>"})
        h = harness(response(200, _thread(incoming)), response(200, SENT))
        assert h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "x"}).successful
        msg = _parsed(h.session.last)
        assert (msg["Subject"], msg["References"]) == ("Re: Long subject", "<x@y> <z@w> <a@b>")


class TestLabels:
    def test_mark_read(self):
        h = harness(response(200, {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"]}))
        result = h.call("GMAIL_ADD_LABEL_TO_EMAIL", {"message_id": "m1", "remove_label_ids": ["unread"]})
        assert h.session.last.json == {"removeLabelIds": ["UNREAD"]}
        assert result.data["labelIds"] == ["INBOX"]

    def test_add_and_remove_with_user_label_names(self):
        h = harness(
            response(200, {"labels": [{"id": "Label_3", "name": "Clients/Acme"}]}),
            response(200, {"id": "m1", "labelIds": ["Label_3", "STARRED"]}),
        )
        h.call("GMAIL_ADD_LABEL_TO_EMAIL", {"message_id": "m1", "add_label_ids": "clients/acme, STARRED", "remove_label_ids": ["INBOX"]})
        assert h.session.last.json == {"addLabelIds": ["Label_3", "STARRED"], "removeLabelIds": ["INBOX"]}

    def test_label_lookup_happens_once(self):
        h = harness(
            response(200, {"labels": [{"id": "Label_1", "name": "A"}, {"id": "Label_2", "name": "B"}]}),
            response(200, {"labels": [{"id": "Label_1", "name": "A"}, {"id": "Label_2", "name": "B"}]}),
            response(200, {"id": "m1"}),
        )
        h.call("GMAIL_ADD_LABEL_TO_EMAIL", {"message_id": "m1", "add_label_ids": ["A"], "remove_label_ids": ["B"]})
        assert [c.path for c in h.session.calls] == ["/me/labels", "/me/labels", "/me/messages/m1/modify"]

    def test_json_encoded_list(self):
        h = harness(response(200, {"id": "t1", "messages": [{"id": "m1", "labelIds": []}, {"id": "m2", "labelIds": []}]}))
        result = h.call("GMAIL_MODIFY_THREAD_LABELS", {"thread_id": "t1", "remove_label_ids": '["INBOX","UNREAD"]'})
        assert h.session.last.json == {"removeLabelIds": ["INBOX", "UNREAD"]}
        assert result.data == {"id": "t1", "messages": [{"id": "m1", "labelIds": []}, {"id": "m2", "labelIds": []}]}

    def test_duplicates_collapse(self):
        h = harness(response(200, {"id": "m1"}))
        h.call("GMAIL_ADD_LABEL_TO_EMAIL", {"message_id": "m1", "add_label_ids": ["STARRED", "starred"]})
        assert h.session.last.json == {"addLabelIds": ["STARRED"]}


class TestTrash:
    def test_trash(self):
        h = harness(response(200, {"id": "m1", "threadId": "t1", "labelIds": ["TRASH"]}))
        result = h.call("GMAIL_MOVE_TO_TRASH", {"messageId": "m1"})
        assert (h.session.last.method, h.session.last.path, h.session.last.json) == ("POST", "/me/messages/m1/trash", None)
        assert result.data["labelIds"] == ["TRASH"]

    def test_trash_is_destructive(self):
        from aisquare_pipe_gmail import GmailToolkit

        spec = GmailToolkit().get_tool("GMAIL_MOVE_TO_TRASH")
        assert spec is not None and spec.destructive and not spec.read_only
