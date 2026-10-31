"""CLI tests for `pipe tools` (against the registered MockToolkit)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from aisquare.pipe.cli.main import cli
from aisquare.pipe.testing.mock_connectors import MOCK_TOOLS, MockToolkit


def invoke(*args: str):
    return CliRunner().invoke(cli, ["tools", *args])


def test_lists_toolkits_without_argument():
    result = invoke()
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("mock "))
    assert row.split() == ["mock", "0.1.0", "oauth2", str(len(MOCK_TOOLS)), "mock"]


def test_lists_toolkits_json():
    result = invoke("--json")
    assert result.exit_code == 0, result.output
    mock = next(row for row in json.loads(result.output) if row["name"] == "mock")
    assert mock["tools"] == len(MOCK_TOOLS) and mock["oauth"] == "mock"


def test_lists_all_slugs_and_descriptions():
    result = invoke("mock")
    assert result.exit_code == 0, result.output
    for tool in MOCK_TOOLS:
        assert tool.slug in result.output
    assert "Permanently delete an item by id." in result.output
    delete_row = next(line for line in result.output.splitlines() if line.startswith("MOCK_DELETE_ITEM"))
    assert "destructive" in delete_row


def test_search_ranks_and_defaults_to_six():
    result = invoke("mock", "--search", "download the file")
    assert result.exit_code == 0, result.output
    rows = [line.split()[0] for line in result.output.splitlines()[2:]]
    assert rows[0] == "MOCK_DOWNLOAD_FILE"
    assert len(rows) <= 6


def test_search_with_limit():
    result = invoke("mock", "-s", "item", "-n", "2")
    assert result.exit_code == 0, result.output
    assert len(result.output.splitlines()[2:]) == 2


def test_limit_without_search_takes_catalog_head():
    result = invoke("mock", "--limit", "1")
    rows = [line.split()[0] for line in result.output.splitlines()[2:]]
    assert rows == [MOCK_TOOLS[0].slug]


def test_json_output_is_full_toolspecs():
    result = invoke("mock", "--json", "--search", "delete item", "--limit", "1")
    assert result.exit_code == 0, result.output
    (tool,) = json.loads(result.output)
    assert tool["slug"] == "MOCK_DELETE_ITEM"
    assert tool["destructive"] is True and tool["input_schema"]["required"] == ["item_id"]


def test_unknown_toolkit_exit_one():
    result = invoke("nope")
    assert result.exit_code == 1
    assert "Toolkit 'nope' not found" in result.output


def test_toolkit_rows_keep_their_keys_and_add_the_card():
    result = invoke("--json")
    assert result.exit_code == 0, result.output
    mock = next(row for row in json.loads(result.output) if row["name"] == "mock")
    assert {"name", "version", "auth", "tools", "oauth", "description"} <= set(mock)
    assert {k: mock[k] for k in MockToolkit.card()} == MockToolkit.card()


def test_tool_json_keeps_its_keys_and_adds_scopes_and_risk():
    result = invoke("mock", "--json")
    assert result.exit_code == 0, result.output
    tools = json.loads(result.output)
    for tool in tools:
        assert set(tool) == {
            "slug", "description", "input_schema", "read_only", "destructive", "tags", "required_scopes", "risk",
        }
    delete = next(t for t in tools if t["slug"] == "MOCK_DELETE_ITEM")
    assert (delete["risk"], delete["required_scopes"]) == ("destructive", [])
