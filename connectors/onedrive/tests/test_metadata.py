"""Router metadata: the connector card, each tool's Graph permissions and
risk, and listing and refusing by the connection's granted scopes (Graph
names compared by short name, case-insensitively, with the file permission
hierarchy)."""

from __future__ import annotations

import pytest
from aisquare.pipe import CATEGORIES

from aisquare_pipe_onedrive import OneDriveToolkit
from aisquare_pipe_onedrive.tools import FILES_READ_ALL, FILES_READWRITE_ALL

from .fakes import CONFIG, harness, response

READS = 9
WRITES = 7


class TestCard:
    def test_card(self):
        card = OneDriveToolkit.card()
        assert (card["name"], card["category"]) == ("onedrive", "files")
        assert card["category"] in CATEGORIES
        assert 2 <= len(card["examples"]) <= 4
        assert {"onedrive", "excel", "folder"} <= set(card["keywords"])


class TestToolScopes:
    def test_reads_need_files_read_all_and_writes_files_readwrite_all(self):
        for tool in OneDriveToolkit().tools():
            assert tool.required_scopes == ((FILES_READ_ALL,) if tool.read_only else (FILES_READWRITE_ALL,)), tool.slug

    def test_risk_follows_the_flags(self):
        for tool in OneDriveToolkit().tools():
            assert tool.risk == ("destructive" if tool.destructive else "read" if tool.read_only else "write")

    def test_the_default_consent_covers_every_tool(self):
        toolkit = OneDriveToolkit()
        assert toolkit.oauth is not None
        assert toolkit.available_tools({**CONFIG, "granted_scopes": list(toolkit.oauth.default_scopes)}) == toolkit.tools()


class TestGrantedScopes:
    @pytest.mark.parametrize(
        ("granted", "listed"),
        [
            ("offline_access User.Read Files.ReadWrite.All", READS + WRITES),
            # Microsoft's token responses spell Graph permissions in full, any case.
            ("https://graph.microsoft.com/files.readwrite.all https://graph.microsoft.com/User.Read", READS + WRITES),
            ("Files.Read.All", READS),
            ("https://graph.microsoft.com/Files.Read.All", READS),
            ("Files.ReadWrite", 0),  # own drive only: the .All tools are not covered
            ("Files.Read User.Read", 0),
            ("", READS + WRITES),  # not known: nothing filtered
        ],
    )
    def test_listing_follows_the_grant(self, granted, listed):
        assert len(OneDriveToolkit().available_tools({**CONFIG, "granted_scopes": granted})) == listed

    def test_a_write_on_a_read_grant_is_refused_before_any_request(self):
        h = harness()
        result = h.call("ONE_DRIVE_DELETE_ITEM", {"item_id": "01ABC"}, config={**CONFIG, "granted_scopes": "Files.Read.All"})
        assert result.error_code == "insufficient_scope"
        assert h.session.calls == []

    def test_a_read_on_a_full_grant_runs(self):
        h = harness(response(200, {"id": "b!drive", "driveType": "personal", "quota": {}}))
        result = h.call("ONE_DRIVE_GET_DRIVE", {}, config={**CONFIG, "granted_scopes": "https://graph.microsoft.com/Files.ReadWrite.All"})
        assert result.successful, result.error
