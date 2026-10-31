"""MIME both ways: the RFC 2822 a send carries (parsed back with the stdlib
parser, as a mail server would) and Gmail payload trees read into text and
attachment lists."""

from __future__ import annotations

import email
import email.policy

import pytest

from aisquare_pipe_gmail import mime
from aisquare_pipe_gmail.rest import JSON_RAW_MAX

from .fakes import (
    attachment_part,
    b64,
    harness,
    message,
    raw_message,
    response,
    text_part,
)

SENT = {"id": "m9", "threadId": "t9", "labelIds": ["SENT"]}


def _send(args: dict) -> email.message.EmailMessage:
    h = harness(response(200, SENT))
    result = h.call("GMAIL_SEND_EMAIL", args)
    assert result.successful, result.error
    parsed = email.message_from_bytes(raw_message(h.session.last), policy=email.policy.default)
    assert isinstance(parsed, email.message.EmailMessage)
    return parsed


class TestBuild:
    def test_plain_message(self):
        msg = _send({"recipient_email": "ann@example.com", "subject": "Hi", "body": "Line one\nLine two"})
        assert (msg["To"], msg["Subject"]) == ("ann@example.com", "Hi")
        assert msg.get_content_type() == "text/plain"
        assert msg.get_content().replace("\r\n", "\n").strip() == "Line one\nLine two"

    def test_html_body_gets_a_text_alternative(self):
        msg = _send(
            {"recipient_email": "ann@example.com", "subject": "Hi", "body": "<h1>News</h1><p>Hello&nbsp;<b>Ann</b></p>", "is_html": True}
        )
        assert msg.get_content_type() == "multipart/alternative"
        plain = msg.get_body(("plain",)).get_content()
        rich = msg.get_body(("html",)).get_content()
        assert plain.split() == ["News", "Hello", "Ann"]
        assert "<b>Ann</b>" in rich

    def test_non_ascii_subject_and_names(self):
        msg = _send({"recipient_email": "Zoë Café <zoe@example.com>", "subject": "Café ☕ — résumé", "body": "naïve ✓"})
        assert msg["Subject"] == "Café ☕ — résumé"
        assert msg["To"].addresses[0].display_name == "Zoë Café"
        assert "naïve ✓" in msg.get_content()

    def test_all_recipient_fields(self):
        msg = _send(
            {
                "to": "ann@example.com, Bob <bob@example.com>",
                "extra_recipients": ["cy@example.com"],
                "cc": "dee@example.com",
                "bcc": ["eve@example.com"],
                "subject": "Team",
                "message_body": "x",
            }
        )
        assert [a.addr_spec for a in msg["To"].addresses] == ["ann@example.com", "bob@example.com", "cy@example.com"]
        assert msg["Cc"] == "dee@example.com"
        assert msg["Bcc"] == "eve@example.com"  # Gmail delivers to Bcc and strips the header

    def test_from_alias(self):
        msg = _send({"recipient_email": "ann@example.com", "body": "x", "from_email": "Sales <sales@example.com>"})
        assert msg["From"] == "Sales <sales@example.com>"

    def test_attachments(self):
        msg = _send(
            {
                "recipient_email": "ann@example.com",
                "subject": "Files",
                "body": "See attached",
                "attachment": [
                    {"name": "report.pdf", "file_content_bytes": "JVBERi0xLjc="},
                    {"name": "../data.csv", "mimetype": "text/csv", "file_content_bytes": b64("a,b\n")},
                ],
            }
        )
        assert msg.get_content_type() == "multipart/mixed"
        files = list(msg.iter_attachments())
        assert [(f.get_filename(), f.get_content_type()) for f in files] == [
            ("report.pdf", "application/pdf"),
            ("data.csv", "text/csv"),
        ]
        assert files[0].get_content() == b"%PDF-1.7"

    def test_single_attachment_object(self):
        msg = _send({"recipient_email": "a@x.co", "attachment": {"name": "a.bin", "file_content_bytes": "AAEC"}})
        (part,) = msg.iter_attachments()
        assert part.get_content() == b"\x00\x01\x02"

    def test_attachments_over_25mb_are_refused(self, monkeypatch):
        monkeypatch.setattr("aisquare_pipe_gmail.tools.ATTACHMENTS_MAX", 10)
        h = harness()
        result = h.call(
            "GMAIL_SEND_EMAIL", {"recipient_email": "a@x.co", "attachment": {"name": "a", "file_content_bytes": b64(b"x" * 11)}}
        )
        assert result.error_code == "invalid_arguments"
        assert h.session.calls == []

    def test_small_message_goes_as_json(self):
        h = harness(response(200, SENT))
        h.call("GMAIL_SEND_EMAIL", {"recipient_email": "a@x.co", "body": "hi"})
        call = h.session.last
        assert (call.upload, call.path, set(call.json)) == (False, "/me/messages/send", {"raw"})

    def test_large_message_goes_as_a_multipart_upload(self):
        big = b64(b"\x01" * (JSON_RAW_MAX + 10))
        h = harness(response(200, SENT))
        result = h.call(
            "GMAIL_SEND_EMAIL", {"recipient_email": "a@x.co", "body": "big", "attachment": {"name": "x.bin", "file_content_bytes": big}}
        )
        assert result.successful, result.error
        call = h.session.last
        assert call.upload and call.path == "/me/messages/send"
        assert call.params == {"uploadType": "multipart"}
        assert call.headers["Content-Type"].startswith("multipart/related; boundary=aisquare-pipe-")
        assert call.data.startswith(b"--aisquare-pipe-")
        assert b'Content-Type: application/json; charset=UTF-8\r\n\r\n{}\r\n' in call.data
        parsed = email.message_from_bytes(raw_message(call), policy=email.policy.default)
        assert next(parsed.iter_attachments()).get_content() == b"\x01" * (JSON_RAW_MAX + 10)

    def test_large_draft_in_a_thread_carries_its_thread_in_the_metadata(self):
        thread = {"id": "t1", "messages": [message("m1", thread_id="t1", extra_headers={"Message_ID": "<a@x>"})]}
        big = b64(b"\x01" * (JSON_RAW_MAX + 10))
        h = harness(response(200, thread), response(200, {"id": "r1", "message": {"id": "m2"}}))
        h.call("GMAIL_CREATE_EMAIL_DRAFT", {"thread_id": "t1", "body": "x", "attachment": {"name": "x", "file_content_bytes": big}})
        assert b'{"message": {"threadId": "t1"}}' in h.session.last.data

    @pytest.mark.parametrize(
        "value", ["ann", "ann@", "@example.com", "ann@@example.com", "a b@example.com", "ann@example.com\r\nX: y"]
    )
    def test_addresses_are_validated(self, value):
        with pytest.raises(ValueError):
            mime.parse_addresses([value], "to")

    @pytest.mark.parametrize(
        ("subject", "expected"), [("Plan", "Re: Plan"), ("Re: Plan", "Re: Plan"), ("RE:Plan", "RE:Plan"), ("", "Re:")]
    )
    def test_reply_subject(self, subject, expected):
        assert mime.reply_subject(subject) == expected


class TestRead:
    def test_plain_text_is_preferred_over_html(self):
        alt = {"partId": "", "mimeType": "multipart/alternative", "filename": "", "headers": [], "body": {"size": 0},
               "parts": [text_part("Plain body"), text_part("<p>Rich body</p>", "text/html", part_id="1")]}
        assert mime.body_text(alt) == ("Plain body", False)

    def test_html_only_becomes_text(self):
        part = text_part(
            "<html><head><style>p{color:red}</style><title>t</title></head><body><p>Hi&nbsp;Ann,</p>"
            "<script>alert(1)</script><ul><li>one</li><li>two</li></ul>Tom &amp; Jerry<br>bye</body></html>",
            "text/html",
        )
        text, _ = mime.body_text(part)
        assert text == "Hi Ann,\none\ntwo\nTom & Jerry\nbye"

    def test_nested_multiparts_and_attachments(self):
        payload = message(
            "m1",
            parts=[
                {"partId": "0", "mimeType": "multipart/related", "filename": "", "headers": [], "body": {"size": 0},
                 "parts": [
                     {"partId": "0.0", "mimeType": "multipart/alternative", "filename": "", "headers": [], "body": {"size": 0},
                      "parts": [text_part("deep text", part_id="0.0.0"), text_part("<b>deep</b>", "text/html", part_id="0.0.1")]},
                     attachment_part("logo.png", "image/png", "ATT-LOGO", 512, part_id="0.1", inline=True),
                 ]},
                attachment_part("Q3.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "ATT-X", 9000, part_id="1"),
            ],
        )["payload"]
        assert mime.body_text(payload) == ("deep text", False)
        assert mime.attachments_of(payload) == [
            {"filename": "logo.png", "mimeType": "image/png", "size": 512, "attachmentId": "ATT-LOGO", "partId": "0.1", "inline": True},
            {"filename": "Q3.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "size": 9000, "attachmentId": "ATT-X", "partId": "1"},
        ]

    @pytest.mark.parametrize("charset", ["iso-8859-1", "windows-1252", "utf-8"])
    def test_part_charset_is_honoured(self, charset):
        assert mime.body_text(text_part("café", charset=charset)) == ("café", False)

    def test_unknown_charset_falls_back_to_utf8(self):
        part = text_part("café")
        part["headers"] = [{"name": "Content-Type", "value": "text/plain; charset=x-klingon"}]
        assert mime.body_text(part) == ("café", False)

    def test_text_attachments_are_not_the_body(self):
        payload = message("m1", parts=[text_part("body"), {**text_part("csv,data"), "filename": "a.csv", "partId": "1"}])["payload"]
        assert mime.body_text(payload) == ("body", False)
        assert [a["filename"] for a in mime.attachments_of(payload)] == ["a.csv"]

    def test_long_text_is_truncated_and_flagged(self):
        h = harness(response(200, message("m1", body="x" * (mime.TEXT_MAX + 5))))
        data = h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": "m1"}).data
        assert len(data["messageText"]) == mime.TEXT_MAX
        assert data["messageTextTruncated"] is True

    def test_headers_are_case_insensitive(self):
        part = {"headers": [{"name": "SUBJECT", "value": "a"}, {"name": "subject", "value": "b"}]}
        assert mime.headers_of(part) == {"subject": "a"}

    def test_garbled_body_data_is_skipped(self):
        part = text_part("x")
        part["body"]["data"] = "***"
        assert mime.body_text(part) == ("", False)
