"""Read tools: search and pagination, message shapes and formats, threads,
attachments as file envelopes, labels and the profile."""

from __future__ import annotations

import base64

import pytest

from .fakes import (
    attachment_part,
    b64,
    google_error,
    harness,
    listing,
    message,
    response,
    text_part,
)


class TestFetchEmails:
    def test_search_reads_each_message_in_full(self):
        h = harness(
            response(200, listing(["m1", "m2"], token="NEXT", estimate=42)),
            response(200, message("m1", subject="One")),
            response(200, message("m2", subject="Two")),
        )
        result = h.call("GMAIL_FETCH_EMAILS", {"query": " from:ann is:unread ", "max_results": 2})
        assert result.successful, result.error
        first = h.session.calls[0]
        assert first.params == {"maxResults": 2, "q": "from:ann is:unread"}
        assert [c.path for c in h.session.calls[1:]] == ["/me/messages/m1", "/me/messages/m2"]
        assert all(c.params == {"format": "full"} for c in h.session.calls[1:])
        data = result.data
        assert list(data)[:2] == ["has_more", "next_page_token"]
        assert (data["has_more"], data["next_page_token"], data["result_size_estimate"]) == (True, "NEXT", 42)
        assert [m["subject"] for m in data["items"]] == ["One", "Two"]

    def test_message_shape_matches_composio(self):
        h = harness(response(200, listing(["m1"])), response(200, message("m1", snippet="It&#39;s here")))
        item = h.call("GMAIL_FETCH_EMAILS", {}).data["items"][0]
        assert item == {
            "messageId": "m1",
            "threadId": "18c1a2b3c4d5e6f0",
            "labelIds": ["INBOX", "UNREAD"],
            "messageTimestamp": "2026-10-31T08:00:00Z",
            "subject": "Q3 report",
            "sender": "Ann <ann@example.com>",
            "to": "me@example.com",
            "date": "Fri, 30 Oct 2026 10:00:00 +0000",
            "preview": {"subject": "Q3 report", "body": "It's here"},
            "messageText": "Hello there",
            "attachmentList": [],
        }

    def test_page_token_continues(self):
        h = harness(response(200, listing(["m3"])), response(200, message("m3")))
        data = h.call("GMAIL_FETCH_EMAILS", {"page_token": "NEXT"}).data
        assert h.session.calls[0].params == {"maxResults": 10, "pageToken": "NEXT"}
        assert (data["has_more"], data["next_page_token"]) == (False, None)

    def test_empty_mailbox(self):
        h = harness(response(200, {"resultSizeEstimate": 0}))
        assert h.call("GMAIL_FETCH_EMAILS", {"query": "nothing"}).data == {
            "has_more": False,
            "next_page_token": None,
            "result_size_estimate": 0,
            "items": [],
        }

    def test_ids_only_skips_the_reads_and_allows_500(self):
        h = harness(response(200, listing(["m1", "m2"])))
        data = h.call("GMAIL_FETCH_EMAILS", {"ids_only": True, "max_results": 500}).data
        assert h.session.calls[0].params["maxResults"] == 500
        assert data["items"] == [{"messageId": "m1", "threadId": "t-m1"}, {"messageId": "m2", "threadId": "t-m2"}]
        assert len(h.session.calls) == 1

    def test_detailed_pages_are_capped_at_100(self):
        h = harness(response(200, listing([])))
        h.call("GMAIL_FETCH_EMAILS", {"max_results": 500})
        assert h.session.calls[0].params["maxResults"] == 100

    def test_verbose_false_reads_headers_only(self):
        meta = message("m1")
        meta["payload"].pop("body")
        h = harness(response(200, listing(["m1"])), response(200, meta))
        item = h.call("GMAIL_FETCH_EMAILS", {"verbose": False}).data["items"][0]
        assert h.session.calls[1].params == {
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Cc", "Subject", "Date"],
        }
        assert "messageText" not in item and "attachmentList" not in item
        assert item["subject"] == "Q3 report"

    def test_include_payload(self):
        h = harness(response(200, listing(["m1"])), response(200, message("m1")))
        item = h.call("GMAIL_FETCH_EMAILS", {"include_payload": True}).data["items"][0]
        assert item["payload"]["mimeType"] == "text/plain"

    def test_spam_and_trash(self):
        h = harness(response(200, listing([])))
        h.call("GMAIL_FETCH_EMAILS", {"include_spam_trash": True})
        assert h.session.calls[0].params["includeSpamTrash"] == "true"

    @pytest.mark.parametrize("labels", [["inbox", "Unread"], "INBOX,UNREAD", '["INBOX", "UNREAD"]'])
    def test_system_labels_in_any_form(self, labels):
        h = harness(response(200, listing([])))
        h.call("GMAIL_FETCH_EMAILS", {"label_ids": labels})
        assert h.session.calls[0].params["labelIds"] == ["INBOX", "UNREAD"]

    def test_user_label_by_name_is_looked_up(self):
        h = harness(
            response(200, {"labels": [{"id": "Label_7", "name": "Receipts"}, {"id": "INBOX", "name": "INBOX"}]}),
            response(200, listing([])),
        )
        h.call("GMAIL_FETCH_EMAILS", {"label_ids": ["receipts", "Label_9"]})
        assert h.session.calls[0].path == "/me/labels"
        assert h.session.calls[1].params["labelIds"] == ["Label_7", "Label_9"]

    def test_unknown_label_name_is_invalid(self):
        h = harness(response(200, {"labels": [{"id": "Label_7", "name": "Receipts"}]}))
        result = h.call("GMAIL_FETCH_EMAILS", {"label_ids": ["Secret project"]})
        assert result.error_code == "invalid_arguments"
        assert "Secret project" not in result.error  # argument values are not echoed

    def test_message_deleted_between_list_and_read_is_skipped(self):
        h = harness(
            response(200, listing(["gone", "m2"])),
            google_error(404, "Requested entity was not found.", "notFound"),
            response(200, message("m2")),
        )
        data = h.call("GMAIL_FETCH_EMAILS", {}).data
        assert [m["messageId"] for m in data["items"]] == ["m2"]

    def test_other_read_failures_fail_the_call(self):
        h = harness(response(200, listing(["m1"])), google_error(400, "Invalid id", "invalidArgument"))
        assert h.call("GMAIL_FETCH_EMAILS", {}).error_code == "invalid_arguments"

    def test_delegated_mailbox(self):
        h = harness(response(200, listing([])))
        h.call("GMAIL_FETCH_EMAILS", {"user_id": "team@example.com"})
        assert h.session.calls[0].path == "/team%40example.com/messages"


class TestFetchMessage:
    def test_full_by_default_with_payload(self):
        h = harness(response(200, message("m1")))
        data = h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": " m1 "}).data
        assert h.session.calls[0].path == "/me/messages/m1"
        assert h.session.calls[0].params == {"format": "full"}
        assert data["messageText"] == "Hello there"
        assert data["payload"]["mimeType"] == "text/plain"

    def test_metadata(self):
        h = harness(response(200, message("m1")))
        data = h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"messageId": "m1", "format": "metadata"}).data
        assert h.session.calls[0].params["metadataHeaders"] == ["From", "To", "Cc", "Subject", "Date"]
        assert "messageText" not in data and "payload" not in data

    def test_minimal(self):
        h = harness(response(200, {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"], "snippet": "hi"}))
        data = h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": "m1", "format": "minimal"}).data
        assert (data["messageId"], data["threadId"], data["subject"], data["preview"]["body"]) == ("m1", "t1", "", "hi")

    def test_raw_is_a_message_rfc822_file(self):
        eml = b"From: a@x.co\r\nSubject: hi\r\n\r\nbody"
        raw = base64.urlsafe_b64encode(eml).decode()
        h = harness(response(200, {"id": "m1", "threadId": "t1", "labelIds": [], "raw": raw}))
        result = h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": "m1", "format": "raw"})
        assert result.data["raw"] == raw and result.data["size"] == len(eml)
        (envelope,) = result.files
        assert (envelope.content_type, envelope.data, envelope.metadata["filename"]) == ("message/rfc822", eml, "m1.eml")

    def test_garbled_raw_is_an_upstream_error(self):
        h = harness(response(200, {"id": "m1", "raw": "%%%"}))
        assert h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": "m1", "format": "raw"}).error_code == "upstream_error"

    def test_id_is_one_path_segment(self):
        h = harness(response(200, message("x")))
        h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": "a/../../profile?x=1"})
        assert h.session.calls[0].path == "/me/messages/a%2F..%2F..%2Fprofile%3Fx%3D1"


class TestThreads:
    def test_list_threads(self):
        h = harness(
            response(200, {"threads": [{"id": "t1", "snippet": "Tom &amp; Ann", "historyId": "5"}], "nextPageToken": "P2", "resultSizeEstimate": 9})
        )
        data = h.call("GMAIL_LIST_THREADS", {"query": "label:work", "max_results": 1}).data
        assert h.session.calls[0].params == {"maxResults": 1, "q": "label:work"}
        assert data == {
            "has_more": True,
            "next_page_token": "P2",
            "result_size_estimate": 9,
            "items": [{"id": "t1", "snippet": "Tom & Ann", "historyId": "5"}],
        }

    def test_list_threads_page_token(self):
        h = harness(response(200, {"threads": []}))
        h.call("GMAIL_LIST_THREADS", {"page_token": "P2"})
        assert h.session.calls[0].params == {"maxResults": 10, "pageToken": "P2"}

    def test_verbose_threads_are_summarised(self):
        detail = {
            "id": "t1",
            "historyId": "5",
            "messages": [
                message("m1", thread_id="t1", subject="Plan", sender="Ann <ann@x.co>", labels=("INBOX",)),
                message("m2", thread_id="t1", subject="Re: Plan", sender="Bob <bob@y.co>", labels=("INBOX", "UNREAD"), internal_date="1793520000000", snippet="ok"),
            ],
        }
        h = harness(response(200, {"threads": [{"id": "t1"}]}), response(200, detail))
        (item,) = h.call("GMAIL_LIST_THREADS", {"verbose": True, "max_results": 300}).data["items"]
        assert h.session.calls[0].params["maxResults"] == 100
        assert h.session.calls[1].params["format"] == "metadata"
        assert item == {
            "id": "t1",
            "snippet": "ok",
            "historyId": "5",
            "subject": "Plan",
            "participants": ["Ann <ann@x.co>", "Bob <bob@y.co>"],
            "messageCount": 2,
            "labelIds": ["INBOX", "UNREAD"],
            "lastMessageTimestamp": "2026-11-01T08:00:00Z",
        }

    def test_get_thread(self):
        thread = {"id": "t1", "historyId": "5", "messages": [message("m1", thread_id="t1"), message("m2", thread_id="t1")]}
        h = harness(response(200, thread))
        data = h.call("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {"threadId": "t1"}).data
        assert h.session.calls[0].params == {"format": "full"}
        assert (data["threadId"], data["messageCount"]) == ("t1", 2)
        assert [m["messageId"] for m in data["messages"]] == ["m1", "m2"]
        assert all("payload" not in m for m in data["messages"])


class TestAttachments:
    def test_attachment_is_a_file_envelope(self):
        pdf = b"%PDF-1.7 binary\x00\xff"
        h = harness(response(200, {"size": len(pdf), "data": b64(pdf)}))
        result = h.call(
            "GMAIL_GET_ATTACHMENT", {"message_id": "m1", "attachment_id": "ANGjdJ8x-_", "file_name": "Q3 report.pdf"}
        )
        assert result.successful, result.error
        assert result.data == {
            "name": "Q3 report.pdf",
            "mimetype": "application/pdf",
            "size": len(pdf),
            "message_id": "m1",
            "attachment_id": "ANGjdJ8x-_",
        }
        (envelope,) = result.files
        assert envelope.data == pdf
        assert envelope.content_type == "application/pdf"
        assert envelope.metadata == {
            "filename": "Q3 report.pdf",
            "mime_type": "application/pdf",
            "message_id": "m1",
            "attachment_id": "ANGjdJ8x-_",
        }

    def test_small_text_is_inlined(self):
        h = harness(response(200, {"size": 9, "data": b64("a,b\n1,2\n")}))
        data = h.call("GMAIL_GET_ATTACHMENT", {"message_id": "m1", "attachment_id": "a1", "file_name": "t.csv"}).data
        assert (data["mimetype"], data["content"]) == ("text/csv", "a,b\n1,2\n")

    def test_non_utf8_text_is_not_inlined(self):
        h = harness(response(200, {"size": 2, "data": b64(b"\xff\xfe")}))
        data = h.call("GMAIL_GET_ATTACHMENT", {"message_id": "m1", "attachment_id": "a1", "file_name": "t.txt"}).data
        assert "content" not in data

    def test_padded_and_unpadded_base64url(self):
        for encoded in (b64(b"hi!?"), base64.urlsafe_b64encode(b"hi!?").decode()):
            h = harness(response(200, {"data": encoded}))
            assert h.call("GMAIL_GET_ATTACHMENT", {"message_id": "m1", "attachment_id": "a"}).files[0].data == b"hi!?"

    @pytest.mark.parametrize(
        ("file_name", "expected"),
        [("../../etc/passwd", "passwd"), ("C:\\Users\\x\\evil.exe", "evil.exe"), ("", "attachment"), ("..", "attachment"), ("a\x00b.txt", "ab.txt")],
    )
    def test_file_name_is_made_safe(self, file_name, expected):
        h = harness(response(200, {"data": b64("x")}))
        result = h.call("GMAIL_GET_ATTACHMENT", {"message_id": "m1", "attachment_id": "a", "file_name": file_name})
        assert result.files[0].metadata["filename"] == result.data["name"] == expected

    def test_unknown_type_is_octet_stream(self):
        h = harness(response(200, {"data": b64("x")}))
        assert h.call("GMAIL_GET_ATTACHMENT", {"message_id": "m1", "attachment_id": "a"}).data["mimetype"] == "application/octet-stream"

    def test_garbled_data_is_an_upstream_error(self):
        h = harness(response(200, {"data": "not*base64"}))
        assert h.call("GMAIL_GET_ATTACHMENT", {"message_id": "m1", "attachment_id": "a"}).error_code == "upstream_error"

    def test_attachment_list_feeds_get_attachment(self):
        parts = [
            {"partId": "0", "mimeType": "multipart/alternative", "filename": "", "headers": [], "body": {"size": 0},
             "parts": [text_part("plain", part_id="0.0"), text_part("<p>rich</p>", "text/html", part_id="0.1")]},
            attachment_part("Q3.pdf", "application/pdf", "ATT-1", 48213, part_id="1"),
        ]
        h = harness(
            response(200, message("m1", parts=parts)),
            response(200, {"size": 3, "data": b64("pdf")}),
        )
        listed = h.call("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": "m1"}).data["attachmentList"]
        assert listed == [{"filename": "Q3.pdf", "mimeType": "application/pdf", "size": 48213, "attachmentId": "ATT-1", "partId": "1"}]
        got = h.call("GMAIL_GET_ATTACHMENT", {"message_id": "m1", "attachment_id": listed[0]["attachmentId"], "file_name": listed[0]["filename"]})
        assert got.files[0].data == b"pdf"
        assert h.session.last.path == "/me/messages/m1/attachments/ATT-1"


class TestLabelsAndProfile:
    def test_list_labels(self):
        labels = [{"id": "INBOX", "name": "INBOX", "type": "system"}, {"id": "Label_7", "name": "Receipts", "type": "user"}]
        h = harness(response(200, {"labels": labels}))
        assert h.call("GMAIL_LIST_LABELS", {}).data == {"has_more": False, "next_page_token": None, "items": labels}

    def test_list_labels_with_details(self):
        h = harness(
            response(200, {"labels": [{"id": "INBOX"}, {"id": "Label_7"}]}),
            response(200, {"id": "INBOX", "messagesTotal": 10, "messagesUnread": 2}),
            response(200, {"id": "Label_7", "messagesTotal": 1, "messagesUnread": 0}),
        )
        items = h.call("GMAIL_LIST_LABELS", {"include_details": True}).data["items"]
        assert [c.path for c in h.session.calls[1:]] == ["/me/labels/INBOX", "/me/labels/Label_7"]
        assert [i["messagesUnread"] for i in items] == [2, 0]

    def test_profile(self):
        profile = {"emailAddress": "me@example.com", "messagesTotal": 1200, "threadsTotal": 800, "historyId": "991"}
        h = harness(response(200, profile))
        assert h.call("GMAIL_GET_PROFILE", {}).data == profile
        assert h.session.calls[0].path == "/me/profile"
