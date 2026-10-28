"""Parity with Composio's Slack toolkit: every slug, argument name and
required argument, against a frozen copy of Composio's tables
(``fixtures/composio_slack_args.json``, Composio toolkit version
20261023_00, from https://docs.composio.dev/toolkits/slack.md).

This copy cannot notice Composio changing: when Composio publishes a new
toolkit version, refresh the fixture from that page and re-run this test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aisquare_pipe_slack.catalog import TOOLS

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "composio_slack_args.json").read_text())
COMPOSIO = FIXTURE["tools"]

CREDENTIAL = "a credential argument: the token comes from the connection's config only"
PREVIEW_IMAGE = "Slack takes it as a multipart image upload, which a tool argument can't carry"

#: Composio arguments deliberately not accepted, and why.
DROPPED: dict[str, dict[str, str]] = {
    "SLACK_UPDATES_A_SLACK_MESSAGE": {"as_user": "legacy: classic apps only"},
    "SLACK_DELETES_A_MESSAGE_FROM_A_CHAT": {"as_user": "legacy: classic apps only"},
    "SLACK_DELETE_SCHEDULED_MESSAGE": {"as_user": "legacy: classic apps only"},
    "SLACK_SEND_EPHEMERAL_MESSAGE": {
        "as_user": "legacy: classic apps only",
        "attachments": "legacy secondary attachments; use blocks",
        "icon_emoji": "needs chat:write.customize, which the app does not request",
        "icon_url": "needs chat:write.customize, which the app does not request",
        "username": "needs chat:write.customize, which the app does not request",
    },
    "SLACK_SCHEDULE_MESSAGE": {"attachments": "legacy secondary attachments; use blocks"},
    "SLACK_SEARCH_MESSAGES": {"auto_paginate": "one page per call, with next_page / next_cursor hints"},
    "SLACK_UPLOAD_OR_CREATE_A_FILE_IN_SLACK": {"token": "a credential argument is never accepted"},
    "SLACK_ADD_REMOTE_FILE": {"preview_image": PREVIEW_IMAGE},
    "SLACK_UPDATE_REMOTE_FILE": {"preview_image": PREVIEW_IMAGE, "token": CREDENTIAL},
    "SLACK_REMOVE_REMOTE_FILE": {"token": CREDENTIAL},
    "SLACK_OPEN_DM": {
        "return_im": "returns the DM's latest message and read state, which is reading; OPEN_DM is "
        "send tier, so it returns the channel ID only (manager ruling)"
    },
}

SLUGS = sorted(COMPOSIO)


def _params(slug: str) -> dict[str, dict]:
    return {p["name"]: p for p in COMPOSIO[slug]["params"]}


#: Tools Composio doesn't have (bookmarks, canvas read/access): named so a
#: missing Composio tool can't hide among them.
EXTRAS = set(FIXTURE["extras"])


def test_the_fixture_is_the_catalog():
    assert FIXTURE["missing"] == []
    assert set(COMPOSIO) | EXTRAS == set(TOOLS)
    assert not set(COMPOSIO) & EXTRAS
    assert EXTRAS == {
        "SLACK_ADD_BOOKMARK", "SLACK_EDIT_BOOKMARK", "SLACK_REMOVE_BOOKMARK", "SLACK_LIST_BOOKMARKS",
        "SLACK_GET_CANVAS_CONTENT", "SLACK_CREATE_CHANNEL_CANVAS", "SLACK_SET_CANVAS_ACCESS",
        "SLACK_DELETE_CANVAS_ACCESS",
    }


@pytest.mark.parametrize("slug", SLUGS)
def test_argument_names_match(slug):
    ours = set(TOOLS[slug].spec.input_schema["properties"])
    theirs = set(_params(slug))
    dropped = set(DROPPED.get(slug, {}))
    assert theirs - dropped == ours, f"{slug}: missing {sorted(theirs - dropped - ours)}, extra {sorted(ours - theirs)}"
    assert dropped <= theirs, f"{slug}: DROPPED names arguments Composio does not have"


@pytest.mark.parametrize("slug", SLUGS)
def test_composio_required_arguments_stay_required(slug):
    # Ours may require more (Composio marks some of Slack's required
    # arguments optional), never less.
    required = set(TOOLS[slug].spec.input_schema["required"])
    theirs = {name for name, p in _params(slug).items() if p["required"]}
    assert theirs <= required


@pytest.mark.parametrize("slug", SLUGS)
def test_argument_types_accept_composio_values(slug):
    properties = TOOLS[slug].spec.input_schema["properties"]
    for name, param in _params(slug).items():
        if name not in properties:
            continue
        declared = properties[name]["type"]
        declared = declared if isinstance(declared, list) else [declared]
        assert param["type"] in declared, f"{slug}.{name}: Composio sends {param['type']}, we take {declared}"


def test_no_credential_arguments():
    for slug, tool in TOOLS.items():
        for name in tool.spec.input_schema["properties"]:
            assert "token" not in name.lower(), slug
