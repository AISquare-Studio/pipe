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


#: An RFC 2047 encoded-word whose text is "Ann\r\nBcc: evil@x.co".
ENCODED_CRLF = "=?utf-8?b?QW5uDQpCY2M6IGV2aWxAeC5jbw==?="


class TestHeaderInjection:
    """Display names are set from Address objects, which skip the SMTP
    policy's line-break check, so the toolkit checks them itself: every C0
    control, DEL and Unicode line break is refused, and encoded-words are
    decoded before the check. Nothing is sent."""

    @staticmethod
    def _refused(args: dict) -> str:
        h = harness()
        result = h.call("GMAIL_SEND_EMAIL", {"body": "x", **args})
        assert result.error_code == "invalid_arguments", result.error
        assert h.session.calls == []
        return result.error

    @pytest.mark.parametrize(
        "char", ["\x0b", "\x0c", "\x00", "\x1c", "\x1d", "\x1e", "\x7f", "\x85", "\u2028", "\u2029", "\r", "\n"]
    )
    def test_control_characters_in_a_display_name(self, char):
        self._refused({"recipient_email": f'"Ann{char}Bcc: evil@x.co" <a@x.co>'})

    @pytest.mark.parametrize("char", ["\x00", "\x7f", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"])
    def test_control_characters_in_the_subject(self, char):
        self._refused({"recipient_email": "a@x.co", "subject": f"Hi{char}there"})

    @pytest.mark.parametrize("field", ["recipient_email", "cc", "bcc", "from_email"])
    def test_an_encoded_crlf_display_name(self, field):
        args = {"recipient_email": "a@x.co", field: f"{ENCODED_CRLF} <b@x.co>"}
        assert "line breaks" in self._refused(args)

    @pytest.mark.parametrize(
        "name",
        [
            "=?utf-8?b?PT91dGYtOD9iP1FXNXVEUXBDWTJNNklHVjJhV3hBZUM1amJ3PT0/PQ==?=",  # ENCODED_CRLF, encoded again
            "=?no-such-charset?q?Ann?=",
        ],
    )
    def test_undecodable_or_doubly_encoded_names(self, name):
        self._refused({"recipient_email": f"{name} <a@x.co>"})

    def test_an_encoded_name_is_decoded_and_sent_re_encoded(self):
        msg = _send({"recipient_email": "=?utf-8?q?J=C3=BCrgen?= <j@x.de>", "body": "x"})
        assert msg["To"].addresses[0].display_name == "Jürgen"
        assert (msg["To"].addresses[0].addr_spec, len(msg["To"].addresses)) == ("j@x.de", 1)

    def test_a_reply_to_an_encoded_crlf_sender_is_refused(self):
        # The reply path takes Reply-To from an untrusted inbound mail.
        incoming = message("m1", thread_id="t1", extra_headers={"Reply_To": f"{ENCODED_CRLF} <a@x.co>", "Message_ID": "<a@b>"})
        h = harness(response(200, {"id": "t1", "messages": [incoming]}))
        result = h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "x"})
        assert result.error_code == "invalid_arguments"
        assert [c.method for c in h.session.calls] == ["GET"]

    def test_a_header_the_generator_refuses_is_invalid_arguments(self, monkeypatch):
        # On Pythons with the CVE-2024-6923 fix the generator raises
        # HeaderWriteError, which is not a ValueError. Older Pythons
        # (requires-python allows them) have no HeaderWriteError: any
        # MessageError stands in.
        import email.errors

        from aisquare_pipe_gmail import tools

        refused = getattr(email.errors, "HeaderWriteError", email.errors.MessageError)

        def refuse(**_: object) -> bytes:
            raise refused("folded header contains newline")

        monkeypatch.setattr(tools, "build_message", refuse)
        self._refused({"recipient_email": "a@x.co"})

    @pytest.mark.parametrize("value", ["Ann\x0bLee <a@x.co>", "Ann\x0cLee <a@x.co>", "a@x.co\x00"])
    def test_parse_addresses_refuses_control_characters(self, value):
        with pytest.raises(ValueError, match="control characters"):
            mime.parse_addresses([value], "to")

    # HTAB is RFC 5322 whitespace, not a control to refuse: it becomes a space.
    def test_a_tabbed_subject_is_sent_with_spaces(self):
        h = harness(response(200, SENT))
        assert h.call("GMAIL_SEND_EMAIL", {"recipient_email": "a@x.co", "subject": "Q3\tplan\t\tdraft", "body": "x"}).successful
        assert email.message_from_bytes(raw_message(h.session.last), policy=email.policy.default)["Subject"] == "Q3 plan  draft"
        assert b"\t" not in raw_message(h.session.last).split(b"\r\n\r\n", 1)[0]

    @pytest.mark.parametrize(
        "recipient", ["Ann\tLee <a@x.co>", '"Ann\tLee" <a@x.co>', "=?utf-8?q?Ann=09Lee?= <a@x.co>"]
    )
    def test_a_tab_in_a_display_name_becomes_a_space(self, recipient):
        # Checked before the stdlib, whose reparse would fold the tab anyway.
        assert mime.parse_addresses([recipient], "to")[0].display_name == "Ann Lee"
        h = harness(response(200, SENT))
        assert h.call("GMAIL_SEND_EMAIL", {"recipient_email": recipient, "body": "x"}).successful
        headers = raw_message(h.session.last).split(b"\r\n\r\n", 1)[0]
        assert b"\t" not in headers and b"To: Ann Lee <a@x.co>" in headers

    @pytest.mark.parametrize("code", [c for c in range(0x20) if c != 0x09] + [0x7F, 0x85, 0x2028, 0x2029])
    def test_check_header_refuses_every_other_control(self, code):
        assert mime.check_header("a\tb", "subject") == "a b"
        with pytest.raises(ValueError, match="control characters"):
            mime.check_header(f"a{chr(code)}b", "subject")



#: ENCODED_CRLF encoded once more, as a sender would to get past one decoding.
DOUBLY_ENCODED = "=?utf-8?b?PT91dGYtOD9iP1FXNXVEUXBDWTJNNklHVjJhV3hBZUM1amJ3PT0/PQ==?="


@pytest.fixture
def unpatched_generator(monkeypatch):
    """Stand in for a Python without the CVE-2024-6923 fix (3.11.0-3.11.9,
    3.12.0-3.12.4). Those write a short header as the standard library parsed
    it, encoded-words decoded and line breaks kept, and their generator
    doesn't check what it writes."""
    import email.headerregistry

    def fold(self, *, policy):
        return f"{self.name}: {self}{policy.linesep}"

    monkeypatch.setattr(email.headerregistry.BaseHeader, "fold", fold)
    if hasattr(email.policy.SMTP, "verify_generated_headers"):  # patched Pythons only
        monkeypatch.setattr(email.policy, "SMTP", email.policy.SMTP.clone(verify_generated_headers=False))


def _injected(raw: bytes) -> bool:
    """Whether the header block got a Bcc line of its own."""
    head = raw.split(b"\r\n\r\n", 1)[0]
    return any(line.lower().startswith(b"bcc:") for line in head.split(b"\r\n"))


def _reply(incoming: dict, **args):
    h = harness(response(200, {"id": "t1", "messages": [incoming]}), response(200, SENT))
    return h, h.call("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "message_body": "x", **args})


class TestEncodedWordsInUnstructuredHeaders:
    """An encoded-word in Subject, In-Reply-To or References is decoded by the
    standard library when the header is set, and an unpatched generator
    writes the decoded CRLF out: a sender could Bcc themselves on the user's
    reply. These headers are decoded and checked like display names, and
    In-Reply-To/References must be msg-ids."""

    def test_the_stand_in_injects_like_an_unpatched_python(self, unpatched_generator):
        msg = email.message.EmailMessage(policy=email.policy.SMTP)
        msg["To"] = "a@x.co"
        msg["Subject"] = ENCODED_CRLF
        msg.set_content("x")
        assert _injected(msg.as_bytes())

    # The tester's three repros (board seq 6072).
    def test_repro_1_caller_subject(self, unpatched_generator):
        h = harness()
        result = h.call("GMAIL_SEND_EMAIL", {"recipient_email": "a@x.co", "subject": ENCODED_CRLF, "body": "x"})
        assert result.error_code == "invalid_arguments" and "line breaks" in result.error
        assert h.session.calls == []

    def test_repro_2_inbound_subject_on_a_reply(self, unpatched_generator):
        h, result = _reply(message("m1", thread_id="t1", subject=ENCODED_CRLF, extra_headers={"Message_ID": "<a@b>"}))
        assert result.error_code == "invalid_arguments" and "give a subject" in result.error
        assert [c.method for c in h.session.calls] == ["GET"]

    def test_repro_3_inbound_message_id_on_a_reply(self, unpatched_generator):
        h, result = _reply(message("m1", thread_id="t1", extra_headers={"Message_ID": f"<a@b> {ENCODED_CRLF}"}))
        assert result.error_code == "invalid_arguments" and "Message-ID" in result.error
        assert [c.method for c in h.session.calls] == ["GET"]

    @pytest.mark.parametrize("value", [ENCODED_CRLF, DOUBLY_ENCODED, f"<{ENCODED_CRLF}@x>", f"<{DOUBLY_ENCODED}@x>"])
    def test_inbound_references_are_checked_too(self, unpatched_generator, value):
        incoming = message("m1", thread_id="t1", extra_headers={"Message_ID": "<a@b>", "References": f"<x@y> {value}"})
        h, result = _reply(incoming)
        assert result.error_code == "invalid_arguments"
        assert [c.method for c in h.session.calls] == ["GET"]

    def test_a_given_subject_replaces_an_unsafe_inbound_one(self, unpatched_generator):
        h, result = _reply(
            message("m1", thread_id="t1", subject=ENCODED_CRLF, extra_headers={"Message_ID": "<a@b>"}), subject="Re: plan"
        )
        assert result.successful, result.error
        assert not _injected(raw_message(h.session.last))

    @pytest.mark.parametrize(
        "subject",
        [
            DOUBLY_ENCODED,  # one decoding still leaves an encoded-word
            "=?x-unknown?q?Ann=0D=0ABcc:_e@x.co?=",  # decode_header can't, the stdlib parser can
            "=?utf-8*en?q?Ann=0D=0ABcc:_e@x.co?=",  # an RFC 2231 language tag: the same
            f"Re: x{ENCODED_CRLF}",  # not delimited by whitespace
        ],
    )
    def test_undecodable_or_doubly_encoded_subjects(self, unpatched_generator, subject):
        h = harness()
        assert h.call("GMAIL_SEND_EMAIL", {"recipient_email": "a@x.co", "subject": subject, "body": "x"}).error_code == "invalid_arguments"
        assert h.session.calls == []

    @pytest.mark.parametrize("field", ["in_reply_to", "references"])
    @pytest.mark.parametrize("value", [f"<{ENCODED_CRLF}@x>", f"<{DOUBLY_ENCODED}@x>"])
    def test_build_message_decodes_msg_id_headers(self, unpatched_generator, field, value):
        # An encoded-word inside <...> is msg-id syntax, so only decoding stops it.
        with pytest.raises(ValueError):
            mime.build_message(to=mime.parse_addresses(["a@x.co"], "to"), subject="x", **{field: value})

    def test_what_the_stdlib_parsed_is_checked_too(self, unpatched_generator, monkeypatch):
        # Second layer: with decoding switched off, the parsed header still
        # carries the CRLF, and build_message refuses it.
        monkeypatch.setattr(mime, "decode_words", lambda value, what: mime.check_header(value, what))
        to = mime.parse_addresses(["a@x.co"], "to")
        for kwargs in ({"subject": ENCODED_CRLF}, {"subject": "x", "references": f"<{ENCODED_CRLF}@x>"}):
            with pytest.raises(ValueError, match="line breaks"):
                mime.build_message(to=to, **kwargs)

    def test_decode_words(self):
        assert mime.decode_words("=?utf-8?q?Caf=C3=A9?= plan", "subject") == "Café plan"
        assert mime.decode_words("50% =? maybe", "subject") == "50% =? maybe"
        for value in (ENCODED_CRLF, DOUBLY_ENCODED, "=?x-unknown?q?Ann?="):
            with pytest.raises(ValueError):
                mime.decode_words(value, "subject")

    # Real stdlib from here on: legitimate encoded-words still work.
    @pytest.mark.parametrize(
        ("inbound", "expected"),
        [("=?utf-8?q?Caf=C3=A9_plan?=", "Re: Café plan"), ("=?utf-8?q?Re:_Plan?=", "Re: Plan")],
    )
    def test_an_encoded_inbound_subject_is_decoded_for_the_reply(self, inbound, expected):
        h, result = _reply(message("m1", thread_id="t1", subject=inbound, extra_headers={"Message_ID": "<a@b>"}))
        assert result.successful, result.error
        sent = email.message_from_bytes(raw_message(h.session.last), policy=email.policy.default)
        assert sent["Subject"] == expected

    def test_malformed_inbound_ids_are_left_out_not_fatal(self):
        incoming = message(
            "m1", thread_id="t1", extra_headers={"Message_ID": "no-brackets@x", "References": "<x@y> (a comment) junk <z@[10.0.0.1]>"}
        )
        h, result = _reply(incoming)
        assert result.successful, result.error
        sent = email.message_from_bytes(raw_message(h.session.last), policy=email.policy.default)
        assert sent["In-Reply-To"] is None
        assert sent["References"] == "<x@y> <z@[10.0.0.1]>"

    @pytest.mark.parametrize(
        ("kwargs", "ok"),
        [
            ({"in_reply_to": "<CAF1.x+y@mail.example.com>", "references": "<a@b> <c.d@[127.0.0.1]>"}, True),
            ({"in_reply_to": "not-a-msg-id"}, False),
            ({"in_reply_to": "<a@b> <c@d>"}, False),  # In-Reply-To is one msg-id
            ({"in_reply_to": "<no-at-sign>"}, False),
            ({"references": "<a@b> junk"}, False),
            ({"references": "<a@b>, <c@d>"}, False),
        ],
    )
    def test_build_message_wants_msg_ids(self, kwargs, ok):
        to = mime.parse_addresses(["a@x.co"], "to")
        if ok:
            assert mime.build_message(to=to, subject="x", **kwargs)
        else:
            with pytest.raises(ValueError, match="msg-id"):
                mime.build_message(to=to, subject="x", **kwargs)

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
