"""Router metadata: the connector card, each tool's OAuth scopes and risk,
and listing and refusing by the connection's granted scopes (with Google's
Drive scope hierarchy)."""

from __future__ import annotations

import pytest
from aisquare.pipe import CATEGORIES

from aisquare_pipe_googledrive import GoogleDriveToolkit
from aisquare_pipe_googledrive.tools import DRIVE_READONLY_SCOPE, DRIVE_SCOPE

from .fakes import CONFIG, file_meta, harness, response

READS = 10
WRITES = 13


class TestCard:
    def test_card(self):
        card = GoogleDriveToolkit.card()
        assert (card["name"], card["category"]) == ("googledrive", "files")
        assert card["category"] in CATEGORIES
        assert 2 <= len(card["examples"]) <= 4
        assert {"google drive", "spreadsheet", "folder"} <= set(card["keywords"])


class TestToolScopes:
    def test_reads_need_drive_readonly_and_writes_need_drive(self):
        for tool in GoogleDriveToolkit().tools():
            assert tool.required_scopes == ((DRIVE_READONLY_SCOPE,) if tool.read_only else (DRIVE_SCOPE,)), tool.slug

    def test_risk_follows_the_flags(self):
        for tool in GoogleDriveToolkit().tools():
            assert tool.risk == ("destructive" if tool.destructive else "read" if tool.read_only else "write")

    def test_counts(self):
        tools = GoogleDriveToolkit().tools()
        assert (sum(t.read_only for t in tools), sum(not t.read_only for t in tools)) == (READS, WRITES)


class TestGrantedScopes:
    @pytest.mark.parametrize(
        ("granted", "listed"),
        [
            ([DRIVE_SCOPE], READS + WRITES),  # the default consent: drive covers drive.readonly
            ([DRIVE_READONLY_SCOPE], READS),
            (["https://www.googleapis.com/auth/drive.file"], 0),  # per-file access covers neither
            (["https://www.googleapis.com/auth/gmail.readonly"], 0),
            (None, READS + WRITES),  # not known: nothing filtered
        ],
    )
    def test_listing_follows_the_grant(self, granted, listed):
        assert len(GoogleDriveToolkit().available_tools({**CONFIG, "granted_scopes": granted})) == listed

    def test_a_write_on_a_read_only_grant_is_refused_before_any_request(self):
        h = harness()
        result = h.call("GOOGLEDRIVE_CREATE_FOLDER", {"name": "x"}, config={**CONFIG, "granted_scopes": [DRIVE_READONLY_SCOPE]})
        assert result.error_code == "insufficient_scope"
        assert h.session.calls == []

    def test_a_read_on_the_full_scope_runs(self):
        h = harness(response(200, file_meta()))
        result = h.call("GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": "file123"}, config={**CONFIG, "granted_scopes": [DRIVE_SCOPE]})
        assert result.successful, result.error
