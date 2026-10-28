"""The breadth tools beyond the per-tool table: the v2 upload, downloads as
DataEnvelopes, canvases, profile photo, DnD and the unread-from-user
composite — end to end through ``call_tool`` with HTTP mocked."""

from __future__ import annotations

import base64
import json

import pytest
from aisquare.pipe import DataEnvelope

from aisquare_pipe_slack.constants import RESULT_CHAR_CAP
from aisquare_pipe_slack.files import DOWNLOAD_MAX_BYTES

from .fakes import (
    ALL_TIERS,
    BOT_CONFIG,
    USER_TOKEN,
    channels_page,
    harness,
    ok,
    response,
    slack_error,
)

C = "C0123456"
UPLOAD_URL = "https://files.slack.com/upload/v1/ABC123"
FILE_URL = "https://files.slack.com/files-pri/T0123-F0123ABCD/download/notes.md"
UPLOAD = "SLACK_UPLOAD_OR_CREATE_A_FILE_IN_SLACK"
DOWNLOAD = "SLACK_DOWNLOAD_SLACK_FILE"


def _upload_replies(file_id: str = "F0UP12345"):
    return [
        ok(upload_url=UPLOAD_URL, file_id=file_id),
        response(200, content=b"OK - 5"),
        ok(files=[{"id": file_id, "name": "notes.md", "title": "Notes"}]),
    ]


class TestUpload:
    def test_three_steps_shared_with_the_marker(self):
        h = harness(*_upload_replies())
        result = h.call(UPLOAD, {"content": "hello", "filename": "notes.md", "title": "Notes",
                                 "channels": C, "initial_comment": "fyi", "thread_ts": "1.2"})
        assert result.successful, result.error
        ticket, post, done = h.session.calls
        assert ticket.api_method == "files.getUploadURLExternal"
        assert ticket.args == {"filename": "notes.md", "length": "5"}
        assert (post.method, post.url, post.data) == ("POST", UPLOAD_URL, b"hello")
        assert "Authorization" not in post.headers  # the URL is pre-signed
        assert done.api_method == "files.completeUploadExternal" and done.is_json
        assert done.args == {
            "files": [{"id": "F0UP12345", "title": "Notes"}],
            "channels": C,
            "initial_comment": "fyi\n_Sent by pipe_",
            "thread_ts": "1.2",
        }
        assert result.data == {"file": {"id": "F0UP12345", "name": "notes.md", "title": "Notes"}, "shared_to": [C]}

    def test_without_channels_the_file_stays_private_and_unmarked(self):
        h = harness(*_upload_replies())
        assert h.call(UPLOAD, {"content": "draft"}).data["shared_to"] == []
        assert h.session.last.args == {"files": [{"id": "F0UP12345"}]}
        assert h.session.calls[0].args["filename"] == "snippet.txt"

    def test_the_bot_shares_without_a_marker(self):
        h = harness(*_upload_replies())
        h.call(UPLOAD, {"content": "x", "channels": C}, BOT_CONFIG)
        assert "initial_comment" not in h.session.last.args

    def test_a_marker_alone_when_there_is_no_comment(self):
        h = harness(*_upload_replies())
        h.call(UPLOAD, {"content": "x", "channels": C})
        assert h.session.last.args["initial_comment"] == "_Sent by pipe_"

    def test_blocks_keep_the_marker_as_a_context_block(self):
        # Slack ignores blocks when initial_comment is set, so the marker
        # goes into the blocks and no initial_comment is sent.
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "see file"}}]
        h = harness(*_upload_replies())
        assert h.call(UPLOAD, {"content": "x", "channels": C, "blocks": blocks}).successful
        sent = h.session.last.args
        assert "initial_comment" not in sent
        assert sent["blocks"] == [
            *blocks,
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Sent by pipe"}]},
        ]

    def test_initial_comment_with_blocks_is_refused(self):
        h = harness()
        result = h.call(UPLOAD, {"content": "x", "channels": C, "initial_comment": "hi",
                                 "blocks": [{"type": "divider"}]})
        assert result.error_code == "invalid_arguments" and "not both" in result.error
        assert h.session.calls == []

    def test_a_full_block_list_leaves_no_room_for_the_marker(self):
        h = harness()
        result = h.call(UPLOAD, {"content": "x", "channels": C, "blocks": [{"type": "divider"}] * 50})
        assert result.error_code == "invalid_arguments" and "at most 49 blocks" in result.error

    def test_thread_ts_needs_one_channel(self):
        h = harness()
        result = h.call(UPLOAD, {"content": "x", "channels": f"{C},C0RANDOM1", "thread_ts": "1.2"})
        assert result.error_code == "invalid_arguments" and "one channel" in result.error
        assert h.session.calls == []  # no upload was started

    def test_channel_names_are_resolved(self):
        h = harness(channels_page({"id": C, "name": "general"}, {"id": "C0RANDOM1", "name": "random"}),
                    channels_page({"id": C, "name": "general"}, {"id": "C0RANDOM1", "name": "random"}),
                    *_upload_replies())
        h.call(UPLOAD, {"content": "x", "channels": "#general, random"})
        assert h.session.last.args["channels"] == f"{C},C0RANDOM1"

    def test_snippet_type_and_blocks(self):
        h = harness(*_upload_replies())
        h.call(UPLOAD, {"content": "print(1)", "filetype": "python", "channels": C,
                        "blocks": '[{"type": "divider"}]'}, BOT_CONFIG)
        assert h.session.calls[0].args["snippet_type"] == "python"
        assert h.session.last.args["blocks"] == [{"type": "divider"}]

    @pytest.mark.parametrize(
        "args, expected",
        [
            ({"file": "s3://staged/x"}, "host-staged file"),
            ({}, "pass content"),
            ({"content": ""}, "pass content"),
            ({"content": "x", "blocks": "{nope"}, "not valid JSON"),
        ],
    )
    def test_bad_input_fails_before_any_request(self, args, expected):
        h = harness()
        result = h.call(UPLOAD, args)
        assert result.error_code == "invalid_arguments"
        assert expected in result.error
        assert h.session.calls == []

    def test_a_non_slack_upload_url_is_refused_before_posting(self):
        h = harness(ok(upload_url="https://evil.test/up", file_id="F0UP12345"))
        result = h.call(UPLOAD, {"content": "x"})
        assert result.successful is False
        assert result.data["slack_error"] == "invalid_upload_url"
        assert len(h.session.calls) == 1

    @pytest.mark.parametrize(
        "replies, failed_at",
        [
            ([slack_error("missing_scope", needed="files:write")], "files.getUploadURLExternal"),
            ([ok(upload_url=UPLOAD_URL, file_id="F1"), response(500, content=b"no")], "files.upload"),
            ([ok(upload_url=UPLOAD_URL, file_id="F1"), response(200), slack_error("channel_not_found")],
             "files.completeUploadExternal"),
            ([ok(file_id="F1")], "files.getUploadURLExternal"),
        ],
    )
    def test_a_failure_at_each_step_names_it(self, replies, failed_at):
        result = harness(*replies).call(UPLOAD, {"content": "x", "channels": C})
        assert result.successful is False
        assert result.error.startswith(f"{failed_at}:")

    def test_a_rate_limited_ticket_is_retried(self):
        h = harness(response(429, headers={"Retry-After": "1"}), *_upload_replies())
        assert h.call(UPLOAD, {"content": "x"}).successful
        assert h.sleeps == [1.0]

    def test_complete_is_not_retried_on_a_server_error(self):
        # completeUploadExternal may be called once only.
        h = harness(ok(upload_url=UPLOAD_URL, file_id="F1"), response(200), slack_error("internal_error"))
        result = h.call(UPLOAD, {"content": "x", "channels": C})
        assert result.error_code == "upstream_error"
        assert h.sleeps == [] and len(h.session.calls) == 3


def _info(**fields):
    base = {"id": "F0123ABCD", "name": "notes.md", "title": "Notes", "mimetype": "text/markdown",
            "size": 11, "url_private_download": FILE_URL, "permalink": "https://acme.slack.com/files/x"}
    return ok(file={**base, **fields})


class TestDownload:
    def test_bytes_come_back_as_a_file(self):
        h = harness(_info(), response(200, content=b"# Notes\nhi\n", headers={"Content-Type": "text/markdown"}))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.successful, result.error
        info, get = h.session.calls
        assert info.api_method == "files.info" and info.args == {"file": "F0123ABCD"}
        assert (get.method, get.url) == ("GET", FILE_URL)
        assert get.headers["Authorization"] == f"Bearer {USER_TOKEN}"
        (envelope,) = result.files
        assert isinstance(envelope, DataEnvelope)
        assert (envelope.content_type, envelope.data) == ("text/markdown", b"# Notes\nhi\n")
        assert envelope.metadata == {"filename": "notes.md", "file_id": "F0123ABCD", "size": 11}
        assert result.data["name"] == "notes.md" and result.data["size"] == 11
        assert result.data["content"] == "# Notes\nhi\n"

    def test_binary_is_not_inlined(self):
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(50))
        h = harness(_info(name="a.png", mimetype="image/png", size=len(png)), response(200, content=png))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert "content" not in result.data
        assert result.files[0].data == png

    def test_external_files_are_refused_without_a_request(self):
        h = harness(_info(is_external=True, url_private="https://drive.example.test/doc"))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.error_code == "invalid_arguments"
        assert "external" in result.error
        assert len(h.session.calls) == 1  # files.info only: the token never goes to the third party

    def test_a_stream_over_the_cap_gives_the_same_refusal(self, monkeypatch):
        monkeypatch.setattr("aisquare_pipe_slack.files.DOWNLOAD_MAX_BYTES", 10)
        h = harness(_info(size=5), response(200, content=b"x" * 64))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.error_code == "invalid_arguments"
        assert result.error == "the file is over the 0 MiB download cap"

    def test_oversized_files_are_refused_before_downloading(self):
        h = harness(_info(size=DOWNLOAD_MAX_BYTES + 1))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.error_code == "invalid_arguments" and "100 MiB" in result.error
        assert len(h.session.calls) == 1

    def test_a_sign_in_page_is_forbidden(self):
        h = harness(_info(), response(200, content=b"<html>Sign in</html>", headers={"Content-Type": "text/html"}))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.error_code == "forbidden"
        assert "files:read" in result.error

    def test_an_html_file_is_fine(self):
        h = harness(_info(name="a.html", mimetype="text/html", size=9),
                    response(200, content=b"<p>hi</p>", headers={"Content-Type": "text/html"}))
        assert h.call(DOWNLOAD, {"file": "F0123ABCD"}).files[0].data == b"<p>hi</p>"

    def test_a_sign_in_page_for_an_html_file_is_still_forbidden(self):
        # The file's type can't tell them apart; the declared size can.
        page = b"<html><title>Slack</title>Sign in to Acme</html>"
        h = harness(_info(name="a.html", mimetype="text/html", size=9),
                    response(200, content=page, headers={"Content-Type": "text/html; charset=utf-8"}))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.error_code == "forbidden" and "sign-in page" in result.error

    BACKSLASH = "https://evil.example\\@files.slack.com/files-pri/x"

    def test_a_backslash_download_url_is_refused_before_any_get(self):
        # urlsplit sees files.slack.com, requests would connect to evil.example.
        h = harness(_info(url_private_download=self.BACKSLASH))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.data == {"slack_error": "invalid_download_url", "status": 200}
        assert len(h.session.calls) == 1

    def test_a_snippet_is_returned_inline(self):
        h = harness(_info(mode="snippet", content="print('hi')", mimetype="text/plain"))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.files[0].data == b"print('hi')"
        assert len(h.session.calls) == 1

    @pytest.mark.parametrize("url", ["https://evil.test/x", "http://files.slack.com/x", None])
    def test_a_download_url_off_files_slack_com_is_refused(self, url):
        h = harness(_info(url_private_download=url, url_private=None))
        result = h.call(DOWNLOAD, {"file": "F0123ABCD"})
        assert result.successful is False and result.data["slack_error"] == "invalid_download_url"
        assert len(h.session.calls) == 1

    def test_a_missing_file_is_not_found(self):
        result = harness(slack_error("file_not_found")).call(DOWNLOAD, {"file": "F0NOPE123"})
        assert result.error_code == "not_found"

    def test_a_failed_get_keeps_its_status(self):
        result = harness(_info(), response(404, content=b"no")).call(DOWNLOAD, {"file": "F0123ABCD"})
        assert (result.error_code, result.data) == ("not_found", {"slack_error": "http_404", "status": 404})

    def test_the_get_is_rate_limited_with_retry_after(self):
        result = harness(_info(), response(429, headers={"Retry-After": "9"})).call(DOWNLOAD, {"file": "F0123ABCD"})
        assert (result.error_code, result.retry_after) == ("rate_limited", 9.0)


class TestFilesAndCanvases:
    def test_list_files_paging_hints(self):
        reply = ok(files=[{"id": "F1"}], paging={"count": 100, "total": 250, "page": 1, "pages": 3})
        result = harness(reply).call("SLACK_LIST_FILES_WITH_FILTERS_IN_SLACK", {"count": 5000})
        assert list(result.data)[:2] == ["has_more", "next_page"]
        assert (result.data["has_more"], result.data["next_page"]) == (True, 2)

    @pytest.mark.parametrize(
        "args, change",
        [
            ({"operation": "replace", "document_content": "# All new"},
             {"operation": "replace", "document_content": {"type": "markdown", "markdown": "# All new"}}),
            ({"operation": "insert_after", "section_id": "temp:C:1", "document_content": "x"},
             {"operation": "insert_after", "section_id": "temp:C:1",
              "document_content": {"type": "markdown", "markdown": "x"}}),
            ({"operation": "delete", "section_id": "temp:C:1"}, {"operation": "delete", "section_id": "temp:C:1"}),
            ({"operation": "rename", "title_content": "New title"},
             {"operation": "rename", "title_content": {"type": "markdown", "markdown": "New title"}}),
        ],
    )
    def test_edit_canvas_builds_one_change(self, args, change):
        h = harness(ok())
        assert h.call("SLACK_EDIT_CANVAS", {"canvas_id": "F0CANVAS1", **args}, enabled_tiers=ALL_TIERS).successful
        assert h.session.last.args == {"canvas_id": "F0CANVAS1", "changes": [change]}

    @pytest.mark.parametrize(
        "args, expected",
        [
            ({"operation": "delete"}, "needs section_id"),
            ({"operation": "insert_before", "document_content": "x"}, "needs section_id"),
            ({"operation": "rename"}, "needs title_content"),
            ({"operation": "insert_at_start"}, "needs document_content"),
            ({"document_content": {"markdown": 5}}, "must be markdown text"),
        ],
    )
    def test_edit_canvas_refuses_incomplete_changes(self, args, expected):
        h = harness()
        result = h.call("SLACK_EDIT_CANVAS", {"canvas_id": "F0CANVAS1", **args}, enabled_tiers=ALL_TIERS)
        assert result.error_code == "invalid_arguments" and expected in result.error
        assert h.session.calls == []

    @pytest.mark.parametrize(
        "args",
        [
            {"access_level": "read"},
            {"access_level": "read", "channel_ids": [C], "user_ids": ["U0123456"]},
            {"access_level": "owner", "channel_ids": [C]},
        ],
    )
    def test_canvas_access_needs_exactly_one_audience(self, args):
        result = harness().call("SLACK_SET_CANVAS_ACCESS", {"canvas_id": "F0CANVAS1", **args}, enabled_tiers=ALL_TIERS)
        assert result.error_code == "invalid_arguments"

    @pytest.mark.parametrize(
        "slug, args, ok_count",
        [
            ("SLACK_SET_CANVAS_ACCESS", {"access_level": "read", "user_ids": [f"U0{i:07d}" for i in range(21)]}, 20),
            ("SLACK_DELETE_CANVAS_ACCESS", {"user_ids": ["U01111111", "U02222222"]}, 1),
            ("SLACK_DELETE_CANVAS_ACCESS", {"channel_ids": [C, "C0RANDOM1"]}, 1),
        ],
    )
    def test_canvas_access_id_limits_are_checked_before_the_call(self, slug, args, ok_count):
        h = harness()
        result = h.call(slug, {"canvas_id": "F0CANVAS1", **args}, enabled_tiers=ALL_TIERS)
        assert result.error_code == "invalid_arguments"
        assert f"at most {ok_count} ID" in result.error
        assert h.session.calls == []

    @pytest.mark.parametrize(
        "content", ["x" * 150_000, "\n" * 99_000, '"' * 60_000, "é" * 120_000], ids=["plain", "newlines", "quotes", "accents"]
    )
    def test_canvas_content_result_fits_the_cap(self, content):
        # The JSON of the result must fit — newlines and quotes take two characters.
        result = harness(ok(content=content)).call("SLACK_GET_CANVAS_CONTENT", {"canvas_id": "F0CANVAS1"})
        data = result.data
        size = len(json.dumps(data, ensure_ascii=False))
        assert size <= RESULT_CHAR_CAP
        assert list(data)[:3] == ["truncated", "total_length", "hint"]
        assert (data["truncated"], data["total_length"]) == (True, len(content))
        assert content.startswith(data["content"])
        # The longest prefix that fits: one more character would not.
        assert len(json.dumps({**data, "content": content[: len(data["content"]) + 1]}, ensure_ascii=False)) > (
            RESULT_CHAR_CAP
        )
        assert "can't be read through this tool" in data["hint"]

    def test_a_canvas_that_fits_is_returned_whole(self):
        short = harness(ok(content="# Plan")).call("SLACK_GET_CANVAS_CONTENT", {"canvas_id": "F0CANVAS1"}).data
        assert short == {"content": "# Plan"}
        fits = "\n" * 49_000  # under the cap even as JSON
        assert "truncated" not in harness(ok(content=fits)).call(
            "SLACK_GET_CANVAS_CONTENT", {"canvas_id": "F0CANVAS1"}
        ).data

    def test_create_channel_canvas_needs_a_channel(self):
        result = harness().call("SLACK_CREATE_CHANNEL_CANVAS", {"channel_id": ""}, enabled_tiers=ALL_TIERS)
        assert result.error_code == "invalid_arguments"

    def test_lookup_sections_takes_json_text(self):
        h = harness(ok(sections=[]))
        h.call("SLACK_LOOKUP_CANVAS_SECTIONS", {"canvas_id": "F0CANVAS1", "criteria": '{"contains_text": "Q4"}'})
        assert h.session.last.args["criteria"] == {"contains_text": "Q4"}
        assert harness().call("SLACK_LOOKUP_CANVAS_SECTIONS", {"canvas_id": "F1", "criteria": {}}).error_code == (
            "invalid_arguments"
        )


class TestPeople:
    def test_profile_photo_is_a_multipart_upload(self):
        png = b"\x89PNG\r\n\x1a\nimage-bytes"
        h = harness(ok())
        image = {"base64": base64.b64encode(png).decode(), "mimetype": "image/png", "name": "me.png"}
        assert h.call("SLACK_SET_PROFILE_PHOTO", {"image": image, "crop_x": 0}, enabled_tiers=ALL_TIERS).successful
        call = h.session.last
        assert call.files == {"image": ("me.png", png, "image/png")}
        assert call.args == {"crop_x": "0"}

    @pytest.mark.parametrize(
        "image, expected",
        [
            ({"s3key": "staged/x"}, "host-staged files are not supported"),
            ({"base64": "not base64!"}, "not valid base64"),
            ({"base64": ""}, "empty"),
            ({"base64": "aGk=", "mimetype": "text/plain"}, "image type"),
        ],
    )
    def test_profile_photo_refuses_bad_images(self, image, expected):
        h = harness()
        result = h.call("SLACK_SET_PROFILE_PHOTO", {"image": image}, enabled_tiers=ALL_TIERS)
        assert result.error_code == "invalid_arguments" and expected in result.error
        assert h.session.calls == []

    @pytest.mark.parametrize("minutes", ["0", "-5", "soon", "1.5"])
    def test_snooze_needs_whole_minutes(self, minutes):
        result = harness().call("SLACK_SET_DND_DURATION", {"num_minutes": minutes}, enabled_tiers=ALL_TIERS)
        assert result.error_code == "invalid_arguments"

    def test_me_message_marker_is_an_italic_last_line(self):
        h = harness(ok())
        h.call("SLACK_SEND_ME_MESSAGE", {"channel": C, "text": "waves"})
        assert h.session.last.args["text"] == "waves\n_Sent by pipe_"

    def test_me_message_as_the_bot_has_no_marker(self):
        h = harness(ok())
        h.call("SLACK_SEND_ME_MESSAGE", {"channel": C, "text": "waves"}, BOT_CONFIG)
        assert h.session.last.args == {"channel": C, "text": "waves"}


class TestUnreadFromUser:
    def test_no_dm_means_nothing_unread_and_nothing_opened(self):
        h = harness(ok(channels=[{"id": "D0OTHER1", "user": "U0BOB1234"}]))
        result = h.call("SLACK_GET_UNREAD_MESSAGES_FROM_USER", {"user_id": "U0SAM1234"})
        assert result.data == {"has_more": False, "next_cursor": None, "channel": None, "messages": []}
        assert h.session.methods == ["users.conversations"]  # never conversations.open
        assert h.session.last.args["types"] == "im"

    def test_a_capped_scan_without_the_dm_refuses(self):
        pages = [ok(channels=[], response_metadata={"next_cursor": f"n{i}"}) for i in range(10)]
        result = harness(*pages).call("SLACK_GET_UNREAD_MESSAGES_FROM_USER", {"user_id": "U0SAM1234"})
        assert result.error_code == "invalid_arguments"

    def test_no_read_position(self):
        h = harness(ok(channels=[{"id": "D0SAM1234", "user": "U0SAM1234"}]), ok(channel={"id": "D0SAM1234"}))
        result = h.call("SLACK_GET_UNREAD_MESSAGES_FROM_USER", {"user_id": "U0SAM1234"})
        assert result.error_code == "invalid_arguments" and "last_read" in result.error
