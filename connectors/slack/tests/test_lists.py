"""Slack Lists and remote files beyond the per-tool table: rich-text
wrapping, argument checks that fail before any request, and their Slack
error codes."""

from __future__ import annotations

from typing import Any

import pytest

from aisquare_pipe_slack.tools_lists import MAX_CELLS, rich_text

from .fakes import ALL_TIERS, BOT_CONFIG, harness, ok, slack_error

LIST = "F0LIST1234"
ROW = "Rec0ROW12345"


def sent(slug: str, args: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    h = harness(ok())
    result = h.call(slug, args, config, enabled_tiers=ALL_TIERS)
    assert result.successful, result.error
    return h.session.last.args


def refused(slug: str, args: dict[str, Any], config: dict[str, Any] | None = None) -> str:
    h = harness()
    result = h.call(slug, args, config, enabled_tiers=ALL_TIERS)
    assert result.successful is False
    assert result.error_code == "invalid_arguments", result.error
    assert h.session.calls == []
    return result.error or ""


class TestRichText:
    @pytest.mark.parametrize(
        "given, blocks",
        [
            ("Q4 work", [rich_text("Q4 work")]),
            ('[{"type": "rich_text", "elements": []}]', [{"type": "rich_text", "elements": []}]),
            ({"type": "rich_text", "elements": []}, [{"type": "rich_text", "elements": []}]),
            ([{"type": "rich_text", "elements": []}], [{"type": "rich_text", "elements": []}]),
            ("[draft] plan", None),  # looks like JSON, isn't: refused, not guessed
        ],
    )
    def test_description_blocks(self, given, blocks):
        if blocks is None:
            assert "not valid JSON" in refused("SLACK_CREATE_SLACK_LIST", {"name": "x", "description_blocks": given})
        else:
            assert sent("SLACK_CREATE_SLACK_LIST", {"name": "x", "description_blocks": given})["description_blocks"] == blocks

    @pytest.mark.parametrize("given", ["  ", [], [{"elements": []}], 5])
    def test_bad_description_blocks(self, given):
        refused("SLACK_UPDATE_SLACK_LIST", {"id": LIST, "description_blocks": given})

    def test_text_cells_are_wrapped_and_others_pass_through(self):
        cells = [
            {"column_id": "Col01", "text": "Ship it"},
            {"column_id": "Col02", "select": ["todo"]},
            {"column_id": "Col03", "text": "kept", "rich_text": [rich_text("given")]},
        ]
        assert sent("SLACK_CREATE_SLACK_LIST_ITEM", {"list_id": LIST, "initial_fields": cells})["initial_fields"] == [
            {"column_id": "Col01", "rich_text": [rich_text("Ship it")]},
            {"column_id": "Col02", "select": ["todo"]},
            {"column_id": "Col03", "text": "kept", "rich_text": [rich_text("given")]},
        ]

    def test_cells_may_come_as_a_json_string(self):
        args = {"list_id": LIST, "cells": f'[{{"row_id": "{ROW}", "column_id": "Col01", "text": "Done"}}]'}
        assert sent("SLACK_UPDATE_SLACK_LIST_ITEM", args)["cells"] == [
            {"row_id": ROW, "column_id": "Col01", "rich_text": [rich_text("Done")]}
        ]


class TestArgumentChecks:
    @pytest.mark.parametrize(
        "slug, args, message",
        [
            ("SLACK_UPDATE_SLACK_LIST", {"id": LIST}, "nothing to change"),
            ("SLACK_UPDATE_SLACK_LIST_ITEM", {"list_id": LIST, "cells": []}, "cells is empty"),
            ("SLACK_UPDATE_SLACK_LIST_ITEM", {"list_id": LIST, "cells": [{"column_id": "Col01", "text": "x"}]},
             "needs the row_id"),
            ("SLACK_UPDATE_SLACK_LIST_ITEM", {"list_id": LIST, "cells": [{"row_id": ROW, "text": "x"}]},
             "needs a column_id"),
            # Core's schema check refuses these before the handler runs.
            ("SLACK_UPDATE_SLACK_LIST_ITEM", {"list_id": LIST, "cells": ["x"]}, "'cells[0]' must be object"),
            ("SLACK_UPDATE_SLACK_LIST_ITEM", {"list_id": LIST, "cells": '["x"]'}, "cells must be an array of objects"),
            ("SLACK_UPDATE_SLACK_LIST_ITEM",
             {"list_id": LIST, "cells": [{"row_id": ROW, "column_id": "C", "text": "x"}] * (MAX_CELLS + 1)},
             f"at most {MAX_CELLS} cells"),
            ("SLACK_CREATE_SLACK_LIST", {"name": "x", "schema": "{not json"}, "schema is not valid JSON"),
            ("SLACK_CREATE_SLACK_LIST", {"name": "x", "schema": [1]}, "'schema[0]' must be object"),
            ("SLACK_CREATE_SLACK_LIST", {"name": "x", "schema": '{"name": "a"}'}, "schema must be an array of objects"),
            ("SLACK_DELETE_MULTIPLE_SLACK_LIST_ITEMS", {"list_id": LIST, "ids": " , "}, "at least one ID"),
            ("SLACK_SET_SLACK_LIST_ACCESS", {"list_id": LIST, "access_level": "read"}, "exactly one of"),
            ("SLACK_SET_SLACK_LIST_ACCESS",
             {"list_id": LIST, "access_level": "read", "user_ids": ["U1"], "channel_ids": ["C1"]}, "exactly one of"),
            ("SLACK_SET_SLACK_LIST_ACCESS", {"list_id": LIST, "access_level": "owner", "channel_ids": ["C1"]},
             "only be granted to users"),
            ("SLACK_DELETE_SLACK_LIST_ACCESS", {"list_id": LIST, "user_ids": []}, "exactly one of"),
        ],
    )
    def test_lists(self, slug, args, message):
        assert message in refused(slug, args)

    def test_only_updates_are_capped(self):
        # Slack documents 100 cells per items.update; it caps neither
        # items.create's initial_fields nor deleteMultiple's ids.
        many = [{"column_id": f"Col{i}", "text": "x"} for i in range(MAX_CELLS + 1)]
        assert len(sent("SLACK_CREATE_SLACK_LIST_ITEM", {"list_id": LIST, "initial_fields": many})["initial_fields"]) == 101
        ids = [f"Rec{i:09d}" for i in range(MAX_CELLS + 1)]
        assert sent("SLACK_DELETE_MULTIPLE_SLACK_LIST_ITEMS", {"list_id": LIST, "ids": ids})["ids"] == ids

    def test_items_page_size_is_clamped(self):
        assert sent("SLACK_LIST_SLACK_LIST_ITEMS", {"list_id": LIST, "limit": 5000})["limit"] == 200
        assert sent("SLACK_LIST_SLACK_LIST_ITEMS", {"list_id": LIST, "limit": 0})["limit"] == 1

    @pytest.mark.parametrize(
        "slug, args, message",
        [
            ("SLACK_GET_REMOTE_FILE", {}, "pass either file"),
            ("SLACK_GET_REMOTE_FILE", {"file": "F1", "external_id": "doc-1"}, "not both"),
            ("SLACK_SHARE_REMOTE_FILE", {"channels": "C0123456"}, "pass either file"),
            ("SLACK_SHARE_REMOTE_FILE", {"file": "F1", "channels": " , "}, "at least one"),
            ("SLACK_REMOVE_REMOTE_FILE", {"file": "F1", "external_id": "doc-1"}, "not both"),
            ("SLACK_UPDATE_REMOTE_FILE", {"file": "F1"}, "nothing to change"),
            ("SLACK_UPDATE_REMOTE_FILE", {"file": "F1", "external_url": "javascript:alert(1)"}, "http(s) URL"),
            ("SLACK_ADD_REMOTE_FILE", {"external_id": "d", "external_url": "file:///etc/passwd", "title": "t"},
             "http(s) URL"),
        ],
    )
    def test_remote_files(self, slug, args, message):
        assert message in refused(slug, args, BOT_CONFIG)


class TestErrors:
    @pytest.mark.parametrize(
        "slack, code, hint",
        [
            ("list_not_found", "not_found", "can't see it"),
            ("record_not_found", "not_found", "can't see it"),
            ("record_deleted", "not_found", "can't see it"),
            ("lists_disabled_user_team", "forbidden", "need a paid plan"),
            ("paid_only", "forbidden", "not allowed"),
            ("over_row_maximum", "conflict", "limit on items"),
            ("over_list_file_maximum", "forbidden", "limit on Lists"),
            ("invalid_column_id", "invalid_arguments", "check the arguments"),
            ("uneditable_column", "invalid_arguments", "check the arguments"),
            ("list_edits_update_error", "upstream_error", "list_edits_update_error"),
        ],
    )
    def test_lists_codes(self, slack, code, hint):
        h = harness(slack_error(slack))
        result = h.call("SLACK_GET_SLACK_LIST_ITEM", {"list_id": LIST, "id": ROW}, enabled_tiers=ALL_TIERS)
        assert result.error_code == code
        assert hint in (result.error or "")

    @pytest.mark.parametrize(
        "slack, code, hint",
        [
            ("bot_user_required", "forbidden", "only the app's bot token"),
            ("no_bot_user_for_app", "forbidden", "has no bot user"),
            ("too_many_ids", "invalid_arguments", "not both"),
            ("file_under_review", "forbidden", "not allowed"),
            ("file_not_found", "not_found", "can't see it"),
        ],
    )
    def test_remote_file_codes(self, slack, code, hint):
        h = harness(slack_error(slack))
        result = h.call("SLACK_ADD_REMOTE_FILE", {"external_id": "d", "external_url": "https://x.example/d", "title": "t"},
                        BOT_CONFIG, enabled_tiers=ALL_TIERS)
        assert result.error_code == code
        assert hint in (result.error or "")

    def test_reads_are_retried_on_server_errors_and_writes_are_not(self):
        h = harness(slack_error("internal_error"), ok(record={"id": ROW}))
        assert h.call("SLACK_GET_SLACK_LIST_ITEM", {"list_id": LIST, "id": ROW}).successful
        assert h.sleeps == [1.0]
        h = harness(slack_error("internal_error"))
        result = h.call("SLACK_CREATE_SLACK_LIST_ITEM", {"list_id": LIST}, enabled_tiers=ALL_TIERS)
        assert result.error_code == "upstream_error" and len(h.session.calls) == 1
