"""Every Slack tool, end to end through ``call_tool`` with HTTP mocked at the
client boundary: the exact methods, body encoding (form vs JSON) and
arguments sent, the happy path, pagination hints, ``*_not_found``,
``invalid_auth``, and a 429. One CASES row per tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from aisquare_pipe_slack import SlackToolkit
from aisquare_pipe_slack.catalog import TOOLS

from .fakes import ALL_TIERS, BOT_CONFIG, harness, ok, response, slack_error

C = "C0123456"
TS = "1761312000.004100"
MARK = "Sent by pipe"


@dataclass(frozen=True)
class Case:
    slug: str
    args: dict[str, Any]
    #: The (last) method called and what it was sent, decoded.
    method: str
    sent: dict[str, Any]
    json: bool = False
    #: Answers to the calls before the last one (composites).
    before: tuple[dict[str, Any], ...] = ()
    reply: dict[str, Any] = field(default_factory=dict)
    paged: str | None = None  # "cursor" or "page"
    #: The connection config (None: the user's); bot-only tools run as the bot.
    config: dict[str, Any] | None = None

    def replies(self, final: Any = None) -> list[Any]:
        return [ok(**b) for b in self.before] + [final if final is not None else ok(**self.reply)]

    @property
    def methods(self) -> list[str]:
        return TOOLS[self.slug].methods[: len(self.before)] + (self.method,) if self.before else (self.method,)

    @property
    def id(self) -> str:
        return self.slug.removeprefix("SLACK_")


MESSAGES = {"messages": [{"type": "message", "ts": TS, "text": "hello"}]}
NO_SEARCH = {"matches": [], "paging": {"count": 20, "total": 0, "page": 1, "pages": 0}}

CASES = [
    # messages
    Case(
        "SLACK_SEND_MESSAGE",
        {"channel": C, "markdown_text": "hi **there**", "thread_ts": TS},
        "chat.postMessage",
        {"channel": C, "thread_ts": TS, "markdown_text": f"hi **there**\n\n_{MARK}_"},
        json=True,
        reply={"channel": C, "ts": "1761400000.000200"},
    ),
    Case(
        "SLACK_UPDATES_A_SLACK_MESSAGE",
        {"channel": C, "ts": TS, "markdown_text": "fixed"},
        "chat.update",
        {"channel": C, "ts": TS, "markdown_text": f"fixed\n\n_{MARK}_"},
        json=True,
        reply={"channel": C, "ts": TS},
    ),
    Case("SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", {"channel": C, "ts": TS}, "chat.delete", {"channel": C, "ts": TS}),
    Case(
        "SLACK_SEND_EPHEMERAL_MESSAGE",
        {"channel": C, "user": "U0123456", "text": "psst"},
        "chat.postEphemeral",
        {"channel": C, "user": "U0123456", "text": f"psst\n_{MARK}_"},
        json=True,
        reply={"message_ts": TS},
    ),
    Case(
        "SLACK_SCHEDULE_MESSAGE",
        {"channel": C, "post_at": "1767225600", "markdown_text": "later"},
        "chat.scheduleMessage",
        {"channel": C, "post_at": 1767225600, "markdown_text": f"later\n\n_{MARK}_"},
        json=True,
        reply={"scheduled_message_id": "Q1298393284", "post_at": 1767225600},
    ),
    Case(
        "SLACK_LIST_SCHEDULED_MESSAGES",
        {},
        "chat.scheduledMessages.list",
        {"limit": "100"},
        reply={"scheduled_messages": []},
        paged="cursor",
    ),
    Case(
        "SLACK_DELETE_SCHEDULED_MESSAGE",
        {"channel": C, "scheduled_message_id": "Q1298393284"},
        "chat.deleteScheduledMessage",
        {"channel": C, "scheduled_message_id": "Q1298393284"},
    ),
    Case(
        "SLACK_RETRIEVE_MESSAGE_PERMALINK_URL",
        {"channel": C, "message_ts": TS},
        "chat.getPermalink",
        {"channel": C, "message_ts": TS},
        reply={"permalink": "https://acme.slack.com/archives/C0123456/p1761312000004100"},
    ),
    # history
    Case(
        "SLACK_FETCH_CONVERSATION_HISTORY",
        {"channel": C, "oldest": "1761000000.000000"},
        "conversations.history",
        {"channel": C, "oldest": "1761000000.000000", "limit": "50"},
        reply=MESSAGES,
        paged="cursor",
    ),
    Case(
        "SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION",
        {"channel": C, "ts": TS},
        "conversations.replies",
        {"channel": C, "ts": TS, "limit": "50"},
        reply=MESSAGES,
        paged="cursor",
    ),
    Case(
        "SLACK_LIST_UNREAD_CHANNEL_MESSAGES",
        {"channel": C},
        "conversations.history",
        {"channel": C, "oldest": "1761000000.000100", "limit": "50"},
        before=({"channel": {"id": C, "last_read": "1761000000.000100", "unread_count_display": 1}},),
        reply=MESSAGES,
        paged="cursor",
    ),
    # search
    Case(
        "SLACK_SEARCH_MESSAGES",
        {"query": "launch in:#product"},
        "search.messages",
        {"query": "launch in:#product", "count": "20"},
        reply={"query": "launch", "messages": NO_SEARCH},
        paged="page",
    ),
    Case(
        "SLACK_SEARCH_FILES",
        {"query": "roadmap", "sort": "timestamp"},
        "search.files",
        {"query": "roadmap", "sort": "timestamp", "count": "20"},
        reply={"query": "roadmap", "files": NO_SEARCH},
        paged="page",
    ),
    Case(
        "SLACK_SEARCH_ALL",
        {"query": "roadmap"},
        "search.all",
        {"query": "roadmap", "count": "20"},
        reply={"query": "roadmap", "messages": NO_SEARCH, "files": NO_SEARCH},
        paged="page",
    ),
    Case(
        "SLACK_FIND_CHANNELS",
        {"query": "product", "limit": 1},
        "conversations.list",
        {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": "200"},
        reply={"channels": [{"id": C, "name": "product"}]},
        paged="cursor",
    ),
    Case(
        "SLACK_FIND_USERS",
        {"search_query": "sam", "limit": 1},
        "users.list",
        {"limit": "200"},
        reply={"members": [{"id": "U0123456", "name": "sam"}]},
        paged="cursor",
    ),
    # conversations
    Case(
        "SLACK_LIST_ALL_CHANNELS",
        {"types": "public_channel,private_channel"},
        "conversations.list",
        {"types": "public_channel,private_channel", "limit": "100"},
        reply={"channels": []},
        paged="cursor",
    ),
    Case(
        "SLACK_LIST_CONVERSATIONS", {}, "users.conversations", {"limit": "100"}, reply={"channels": []}, paged="cursor"
    ),
    Case(
        "SLACK_RETRIEVE_CONVERSATION_INFORMATION",
        {"channel": C, "include_num_members": True},
        "conversations.info",
        {"channel": C, "include_num_members": "true"},
        reply={"channel": {"id": C, "name": "product"}},
    ),
    Case(
        "SLACK_RETRIEVE_CONVERSATION_MEMBERS_LIST",
        {"channel": C},
        "conversations.members",
        {"channel": C, "limit": "100"},
        reply={"members": ["U0123456"]},
        paged="cursor",
    ),
    Case(
        "SLACK_CREATE_CHANNEL",
        {"name": "project-apollo", "is_private": True},
        "conversations.create",
        {"name": "project-apollo", "is_private": "true"},
        reply={"channel": {"id": C}},
    ),
    Case("SLACK_JOIN_AN_EXISTING_CONVERSATION", {"channel": C}, "conversations.join", {"channel": C}),
    Case("SLACK_LEAVE_CONVERSATION", {"channel": C}, "conversations.leave", {"channel": C}),
    Case(
        "SLACK_INVITE_USERS_TO_A_SLACK_CHANNEL",
        {"channel": C, "users": ["U1111111", "U2222222"]},
        "conversations.invite",
        {"channel": C, "users": "U1111111,U2222222"},
    ),
    Case(
        "SLACK_REMOVE_USER_FROM_CONVERSATION",
        {"channel": C, "user": "U0123456"},
        "conversations.kick",
        {"channel": C, "user": "U0123456"},
    ),
    Case("SLACK_ARCHIVE_CONVERSATION", {"channel": C}, "conversations.archive", {"channel": C}),
    Case("SLACK_UNARCHIVE_CHANNEL", {"channel": C}, "conversations.unarchive", {"channel": C}),
    Case(
        "SLACK_RENAME_CONVERSATION",
        {"channel": C, "name": "apollo-2"},
        "conversations.rename",
        {"channel": C, "name": "apollo-2"},
    ),
    Case(
        "SLACK_SET_THE_TOPIC_OF_A_CONVERSATION",
        {"channel": C, "topic": "Launch on Nov 18"},
        "conversations.setTopic",
        {"channel": C, "topic": "Launch on Nov 18"},
    ),
    Case(
        "SLACK_SET_CONVERSATION_PURPOSE",
        {"channel": C, "purpose": "Ship Apollo"},
        "conversations.setPurpose",
        {"channel": C, "purpose": "Ship Apollo"},
    ),
    # DMs
    Case(
        "SLACK_OPEN_DM",
        {"users": "U0123456"},
        "conversations.open",
        {"users": "U0123456"},
        reply={"channel": {"id": "D0123456"}, "already_open": True},
    ),
    Case("SLACK_CLOSE_DM", {"channel": "D0123456"}, "conversations.close", {"channel": "D0123456"}),
    # reactions
    Case(
        "SLACK_ADD_REACTION_TO_AN_ITEM",
        {"channel": C, "timestamp": TS, "name": ":+1:"},
        "reactions.add",
        {"channel": C, "timestamp": TS, "name": "+1"},
    ),
    Case(
        "SLACK_REMOVE_REACTION_FROM_ITEM",
        {"channel": C, "timestamp": TS, "name": "eyes"},
        "reactions.remove",
        {"channel": C, "timestamp": TS, "name": "eyes"},
    ),
    Case(
        "SLACK_FETCH_ITEM_REACTIONS",
        {"channel": C, "timestamp": TS},
        "reactions.get",
        {"channel": C, "timestamp": TS},
        reply={"type": "message", "message": {"reactions": [{"name": "+1", "count": 2}]}},
    ),
    Case("SLACK_LIST_USER_REACTIONS", {}, "reactions.list", {"limit": "100"}, reply={"items": []}, paged="cursor"),
    # pins
    Case("SLACK_PIN_ITEM", {"channel": C, "timestamp": TS}, "pins.add", {"channel": C, "timestamp": TS}),
    Case("SLACK_UNPIN_ITEM", {"channel": C, "timestamp": TS}, "pins.remove", {"channel": C, "timestamp": TS}),
    Case("SLACK_LIST_PINNED_ITEMS", {"channel": C}, "pins.list", {"channel": C}, reply={"items": []}),
    # users
    Case("SLACK_LIST_ALL_USERS", {}, "users.list", {"limit": "100"}, reply={"members": []}, paged="cursor"),
    Case(
        "SLACK_RETRIEVE_DETAILED_USER_INFORMATION",
        {"user": "U0123456"},
        "users.info",
        {"user": "U0123456"},
        reply={"user": {"id": "U0123456"}},
    ),
    Case(
        "SLACK_FIND_USER_BY_EMAIL_ADDRESS",
        {"email": "sam@acme.test"},
        "users.lookupByEmail",
        {"email": "sam@acme.test"},
        reply={"user": {"id": "U0123456"}},
    ),
    Case(
        "SLACK_RETRIEVE_USER_PROFILE_INFORMATION",
        {"include_labels": True},
        "users.profile.get",
        {"include_labels": "true"},
        reply={"profile": {"real_name": "Sam"}},
    ),
    Case(
        "SLACK_SET_USER_PROFILE",
        {"profile": {"status_text": "Lunch", "status_emoji": ":taco:"}},
        "users.profile.set",
        {"profile": {"status_text": "Lunch", "status_emoji": ":taco:"}},
        json=True,
        reply={"profile": {"status_text": "Lunch"}},
    ),
    Case(
        "SLACK_GET_USER_PRESENCE",
        {"user": "U0123456"},
        "users.getPresence",
        {"user": "U0123456"},
        reply={"presence": "active"},
    ),
    Case("SLACK_SET_USER_PRESENCE", {"presence": "away"}, "users.setPresence", {"presence": "away"}),
    # workspace
    Case("SLACK_FETCH_TEAM_INFO", {}, "team.info", {}, reply={"team": {"id": "T0123456", "name": "Acme"}}),
    Case("SLACK_LIST_CUSTOM_EMOJIS", {}, "emoji.list", {}, reply={"emoji": {"shipit": "https://e/shipit.png"}}),
    Case("SLACK_TEST_AUTH", {}, "auth.test", {}, reply={"user_id": "U0123456", "team_id": "T0123456"}),
    Case(
        "SLACK_WHO_AM_I",
        {},
        "team.info",
        {},
        before=({"user": "sam", "user_id": "U0123456", "team": "Acme", "team_id": "T0123456"},),
        reply={"team": {"id": "T0123456", "name": "Acme", "domain": "acme"}},
    ),
    Case("SLACK_API_TEST", {"foo": "bar"}, "api.test", {"foo": "bar"}, reply={"args": {"foo": "bar"}}),
    # -- breadth (S2) --------------------------------------------------------
    Case(
        "SLACK_SEND_ME_MESSAGE",
        {"channel": C, "text": "is reviewing the deck"},
        "chat.meMessage",
        {"channel": C, "text": f"is reviewing the deck\n_{MARK}_"},
        reply={"channel": C, "ts": TS},
    ),
    Case(
        "SLACK_GET_UNREAD_MESSAGES_FROM_USER",
        {"user_id": "U0SAM1234"},
        "conversations.history",
        {"channel": "D0SAM1234", "oldest": "1761000000.000100", "limit": "50"},
        before=(
            {"channels": [{"id": "D0OTHER1", "user": "U0BOB1234"}, {"id": "D0SAM1234", "user": "U0SAM1234"}]},
            {"channel": {"id": "D0SAM1234", "last_read": "1761000000.000100"}},
        ),
        reply=MESSAGES,
        paged="cursor",
    ),
    Case("SLACK_SET_READ_CURSOR_IN_A_CONVERSATION", {"channel": C, "ts": TS}, "conversations.mark", {"channel": C, "ts": TS}),
    Case(
        "SLACK_ADD_BOOKMARK",
        {"channel_id": C, "title": "Roadmap", "link": "https://example.test/roadmap", "emoji": ":map:"},
        "bookmarks.add",
        {"channel_id": C, "title": "Roadmap", "link": "https://example.test/roadmap", "emoji": ":map:", "type": "link"},
        reply={"bookmark": {"id": "Bk0123ABCD"}},
    ),
    Case(
        "SLACK_EDIT_BOOKMARK",
        {"channel_id": C, "bookmark_id": "Bk0123ABCD", "title": "Roadmap v2"},
        "bookmarks.edit",
        {"channel_id": C, "bookmark_id": "Bk0123ABCD", "title": "Roadmap v2"},
    ),
    Case(
        "SLACK_REMOVE_BOOKMARK",
        {"channel_id": C, "bookmark_id": "Bk0123ABCD"},
        "bookmarks.remove",
        {"channel_id": C, "bookmark_id": "Bk0123ABCD"},
    ),
    Case("SLACK_LIST_BOOKMARKS", {"channel_id": C}, "bookmarks.list", {"channel_id": C}, reply={"bookmarks": []}),
    Case("SLACK_GET_TEAM_PROFILE", {"visibility": "visible"}, "team.profile.get", {"visibility": "visible"},
         reply={"profile": {"fields": []}}),
    Case(
        "SLACK_SET_PROFILE_PHOTO",
        {"image": {"base64": "iVBORw0KGgo=", "mimetype": "image/png"}, "crop_w": 512},
        "users.setPhoto",
        {"crop_w": "512"},
    ),
    Case("SLACK_DELETE_USER_PROFILE_PHOTO", {}, "users.deletePhoto", {}),
    Case("SLACK_GET_BOT_USER", {"bot": "B0123ABCD"}, "bots.info", {"bot": "B0123ABCD"}, reply={"bot": {"id": "B0123ABCD"}}),
    Case("SLACK_RETRIEVE_CURRENT_USER_DND_STATUS", {}, "dnd.info", {}, reply={"dnd_enabled": False}),
    Case(
        "SLACK_GET_USER_DND_STATUS",
        {"users": ["U1111111", "U2222222"]},
        "dnd.teamInfo",
        {"users": "U1111111,U2222222"},
        reply={"users": {}},
    ),
    Case("SLACK_SET_DND_DURATION", {"num_minutes": "60"}, "dnd.setSnooze", {"num_minutes": "60"},
         reply={"snooze_enabled": True}),
    Case("SLACK_END_DND", {}, "dnd.endDnd", {}),
    Case("SLACK_END_SNOOZE", {}, "dnd.endSnooze", {}, reply={"snooze_enabled": False}),
    Case(
        "SLACK_RETRIEVE_DETAILED_INFORMATION_ABOUT_A_FILE",
        {"file": "F0123ABCD"},
        "files.info",
        {"file": "F0123ABCD", "limit": "100"},
        reply={"file": {"id": "F0123ABCD"}, "comments": []},
        paged="cursor",
    ),
    Case(
        "SLACK_LIST_FILES_WITH_FILTERS_IN_SLACK",
        {"types": "pdfs", "channel": C},
        "files.list",
        {"types": "pdfs", "channel": C},
        reply={"files": [], "paging": {"count": 100, "total": 0, "page": 1, "pages": 1}},
    ),
    Case("SLACK_DELETE_FILE", {"file": "F0123ABCD"}, "files.delete", {"file": "F0123ABCD"}),
    Case("SLACK_DELETE_FILE_COMMENT", {"file": "F0123ABCD", "id": "Fc0123ABCD"}, "files.comments.delete",
         {"file": "F0123ABCD", "id": "Fc0123ABCD"}),
    Case("SLACK_ENABLE_PUBLIC_SHARING_OF_A_FILE", {"file": "F0123ABCD"}, "files.sharedPublicURL", {"file": "F0123ABCD"},
         reply={"file": {"permalink_public": "https://slack-files.com/T-F-x"}}),
    Case("SLACK_REVOKE_FILE_PUBLIC_SHARING", {"file": "F0123ABCD"}, "files.revokePublicURL", {"file": "F0123ABCD"}),
    Case(
        "SLACK_CREATE_CANVAS",
        {"title": "Plan", "document_content": "# Plan"},
        "canvases.create",
        {"title": "Plan", "document_content": {"type": "markdown", "markdown": "# Plan"}},
        json=True,
        reply={"canvas_id": "F0CANVAS1"},
    ),
    Case(
        "SLACK_EDIT_CANVAS",
        {"canvas_id": "F0CANVAS1", "document_content": {"type": "markdown", "markdown": "- one more"}},
        "canvases.edit",
        {
            "canvas_id": "F0CANVAS1",
            "changes": [{"operation": "insert_at_end", "document_content": {"type": "markdown", "markdown": "- one more"}}],
        },
        json=True,
    ),
    Case("SLACK_DELETE_CANVAS", {"canvas_id": "F0CANVAS1"}, "canvases.delete", {"canvas_id": "F0CANVAS1"}),
    Case(
        "SLACK_LOOKUP_CANVAS_SECTIONS",
        {"canvas_id": "F0CANVAS1", "criteria": {"section_types": ["any_header"], "contains_text": "Agenda"}},
        "canvases.sections.lookup",
        {"canvas_id": "F0CANVAS1", "criteria": {"section_types": ["any_header"], "contains_text": "Agenda"}},
        json=True,
        reply={"sections": [{"id": "temp:C:abc"}]},
    ),
    Case("SLACK_GET_CANVAS_CONTENT", {"canvas_id": "F0CANVAS1"}, "canvases.getContent", {"canvas_id": "F0CANVAS1"},
         reply={"content": "# Plan"}),
    Case(
        "SLACK_CREATE_CHANNEL_CANVAS",
        {"channel_id": C, "document_content": "# Welcome"},
        "conversations.canvases.create",
        {"document_content": {"type": "markdown", "markdown": "# Welcome"}, "channel_id": C},
        json=True,
        reply={"canvas_id": "F0CANVAS2"},
    ),
    Case(
        "SLACK_SET_CANVAS_ACCESS",
        {"canvas_id": "F0CANVAS1", "access_level": "read", "user_ids": ["U0123456"]},
        "canvases.access.set",
        {"canvas_id": "F0CANVAS1", "user_ids": ["U0123456"], "access_level": "read"},
        json=True,
    ),
    Case(
        "SLACK_DELETE_CANVAS_ACCESS",
        {"canvas_id": "F0CANVAS1", "channel_ids": [C]},
        "canvases.access.delete",
        {"canvas_id": "F0CANVAS1", "channel_ids": [C]},
        json=True,
    ),
    Case(
        "SLACK_CREATE_A_REMINDER",
        {"text": "standup", "time": "in 15 minutes"},
        "reminders.add",
        {"text": "standup", "time": "in 15 minutes"},
        reply={"reminder": {"id": "Rm0123ABCD"}},
    ),
    Case("SLACK_GET_REMINDER", {"reminder": "Rm0123ABCD"}, "reminders.info", {"reminder": "Rm0123ABCD"},
         reply={"reminder": {"id": "Rm0123ABCD"}}),
    Case("SLACK_LIST_REMINDERS", {}, "reminders.list", {}, reply={"reminders": []}),
    Case("SLACK_DELETE_REMINDER", {"reminder": "Rm0123ABCD"}, "reminders.delete", {"reminder": "Rm0123ABCD"}),
    Case(
        "SLACK_CREATE_USER_GROUP",
        {"name": "Design", "handle": "design", "include_count": True},
        "usergroups.create",
        {"name": "Design", "handle": "design", "include_count": "true"},
        reply={"usergroup": {"id": "S0123ABCD"}},
    ),
    Case(
        "SLACK_UPDATE_USER_GROUP",
        {"usergroup": "S0123ABCD", "description": "Design team"},
        "usergroups.update",
        {"usergroup": "S0123ABCD", "description": "Design team"},
    ),
    Case("SLACK_DISABLE_USER_GROUP", {"usergroup": "S0123ABCD"}, "usergroups.disable", {"usergroup": "S0123ABCD"}),
    Case("SLACK_ENABLE_USER_GROUP", {"usergroup": "S0123ABCD"}, "usergroups.enable", {"usergroup": "S0123ABCD"}),
    Case("SLACK_LIST_USER_GROUPS", {"include_users": True}, "usergroups.list", {"include_users": "true"},
         reply={"usergroups": []}),
    Case("SLACK_LIST_USER_GROUP_MEMBERS", {"usergroup": "S0123ABCD"}, "usergroups.users.list",
         {"usergroup": "S0123ABCD"}, reply={"users": ["U0123456"]}),
    Case(
        "SLACK_UPDATE_USER_GROUP_MEMBERS",
        {"usergroup": "S0123ABCD", "users": "U1111111, U2222222"},
        "usergroups.users.update",
        {"usergroup": "S0123ABCD", "users": "U1111111,U2222222"},
    ),
    Case("SLACK_LIST_AUTH_TEAMS", {}, "auth.teams.list", {"limit": "100"}, reply={"teams": []}, paged="cursor"),
]
#: Tools whose calls leave the Web API (the upload POST, the file GET): their
#: own tests in test_breadth.py cover the same ground.
SPECIAL = {"SLACK_UPLOAD_OR_CREATE_A_FILE_IN_SLACK", "SLACK_DOWNLOAD_SLACK_FILE"}

# Slack Lists and remote files
LIST = "F0LIST1234"
ROW = "Rec0ROW12345"
REMOTE = "F0REMOTE12"


def _rich(text: str) -> list[dict[str, Any]]:
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}]}]


CASES += [
    Case(
        "SLACK_CREATE_SLACK_LIST",
        {"name": "Sprint", "description_blocks": "Q4 work", "todo_mode": True},
        "slackLists.create",
        {"name": "Sprint", "description_blocks": _rich("Q4 work"), "todo_mode": True},
        json=True,
        reply={"list_id": LIST},
    ),
    Case(
        "SLACK_UPDATE_SLACK_LIST",
        {"id": LIST, "name": "Sprint 2"},
        "slackLists.update",
        {"id": LIST, "name": "Sprint 2"},
        json=True,
    ),
    Case(
        "SLACK_CREATE_SLACK_LIST_ITEM",
        {"list_id": LIST, "initial_fields": [{"column_id": "Col01", "text": "Ship it"},
                                             {"column_id": "Col02", "select": ["todo"]}]},
        "slackLists.items.create",
        {"list_id": LIST, "initial_fields": [{"column_id": "Col01", "rich_text": _rich("Ship it")},
                                             {"column_id": "Col02", "select": ["todo"]}]},
        json=True,
        reply={"item": {"id": ROW, "list_id": LIST}},
    ),
    Case(
        "SLACK_GET_SLACK_LIST_ITEM",
        {"list_id": LIST, "id": ROW},
        "slackLists.items.info",
        {"list_id": LIST, "id": ROW},
        json=True,
        reply={"record": {"id": ROW}, "list": {"id": LIST}},
    ),
    Case(
        "SLACK_LIST_SLACK_LIST_ITEMS",
        {"list_id": LIST},
        "slackLists.items.list",
        {"list_id": LIST, "limit": 50},
        json=True,
        reply={"items": [{"id": ROW, "fields": []}]},
        paged="cursor",
    ),
    Case(
        "SLACK_UPDATE_SLACK_LIST_ITEM",
        {"list_id": LIST, "cells": [{"row_id": ROW, "column_id": "Col02", "select": ["done"]}]},
        "slackLists.items.update",
        {"list_id": LIST, "cells": [{"row_id": ROW, "column_id": "Col02", "select": ["done"]}]},
        json=True,
    ),
    Case(
        "SLACK_DELETE_SLACK_LIST_ITEM",
        {"list_id": LIST, "id": ROW},
        "slackLists.items.delete",
        {"list_id": LIST, "id": ROW},
        json=True,
    ),
    Case(
        "SLACK_DELETE_MULTIPLE_SLACK_LIST_ITEMS",
        {"list_id": LIST, "ids": "Rec0ROW12345, Rec0ROW67890"},
        "slackLists.items.deleteMultiple",
        {"list_id": LIST, "ids": ["Rec0ROW12345", "Rec0ROW67890"]},
        json=True,
    ),
    Case(
        "SLACK_SET_SLACK_LIST_ACCESS",
        {"list_id": LIST, "access_level": "write", "channel_ids": [C]},
        "slackLists.access.set",
        {"list_id": LIST, "access_level": "write", "channel_ids": [C]},
        json=True,
    ),
    Case(
        "SLACK_DELETE_SLACK_LIST_ACCESS",
        {"list_id": LIST, "user_ids": "U0123456"},
        "slackLists.access.delete",
        {"list_id": LIST, "user_ids": ["U0123456"]},
        json=True,
    ),
    Case(
        "SLACK_ADD_REMOTE_FILE",
        {"external_id": "doc-1", "external_url": "https://docs.example.com/d/1", "title": "Plan"},
        "files.remote.add",
        {"external_id": "doc-1", "external_url": "https://docs.example.com/d/1", "title": "Plan"},
        reply={"file": {"id": REMOTE, "is_external": True}},
        config=BOT_CONFIG,
    ),
    Case(
        "SLACK_UPDATE_REMOTE_FILE",
        {"external_id": "doc-1", "title": "Plan v2"},
        "files.remote.update",
        {"external_id": "doc-1", "title": "Plan v2"},
        reply={"file": {"id": REMOTE, "title": "Plan v2"}},
        config=BOT_CONFIG,
    ),
    Case(
        "SLACK_REMOVE_REMOTE_FILE",
        {"file": REMOTE},
        "files.remote.remove",
        {"file": REMOTE},
        config=BOT_CONFIG,
    ),
    Case(
        "SLACK_GET_REMOTE_FILE",
        {"file": REMOTE},
        "files.remote.info",
        {"file": REMOTE},
        reply={"file": {"id": REMOTE, "external_url": "https://docs.example.com/d/1"}},
    ),
    Case(
        "SLACK_LIST_REMOTE_FILES",
        {"channel": C, "ts_from": 1761000000},
        "files.remote.list",
        {"channel": C, "ts_from": "1761000000", "limit": "100"},
        reply={"files": []},
        paged="cursor",
    ),
    Case(
        "SLACK_SHARE_REMOTE_FILE",
        {"file": REMOTE, "channels": [C, "D0123456"]},
        "files.remote.share",
        {"file": REMOTE, "channels": f"{C},D0123456"},
        reply={"file": {"id": REMOTE}},
        config=BOT_CONFIG,
    ),
]

IDS = [c.id for c in CASES]
CURSOR_PAGED = [c for c in CASES if c.paged == "cursor"]
PAGE_PAGED = [c for c in CASES if c.paged == "page"]


def _call(h, case: Case, **overrides: Any):
    return h.call(case.slug, case.args, case.config, enabled_tiers=ALL_TIERS, **overrides)


def test_cases_cover_every_tool_exactly_once():
    assert sorted([c.slug for c in CASES] + sorted(SPECIAL)) == sorted(t.slug for t in SlackToolkit().tools())


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_happy_path(case: Case):
    h = harness(*case.replies())
    result = _call(h, case)
    assert result.successful, result.error
    assert (result.error, result.error_code, result.retry_after) == (None, None, None)
    assert h.session.methods == list(case.methods)
    call = h.session.last
    assert call.method == "POST"
    assert call.allow_redirects is False
    assert call.is_json is case.json
    assert call.args == case.sent
    assert isinstance(result.data, dict)
    assert "ok" not in result.data and "response_metadata" not in result.data
    if case.paged == "cursor":
        assert list(result.data)[:2] == ["has_more", "next_cursor"]
        assert (result.data["has_more"], result.data["next_cursor"]) == (False, None)
    if case.paged == "page":
        assert list(result.data)[:2] == ["has_more", "next_page"]
        assert (result.data["has_more"], result.data["next_page"]) == (False, None)


@pytest.mark.parametrize("case", CURSOR_PAGED, ids=[c.id for c in CURSOR_PAGED])
def test_cursor_hint(case: Case):
    final = ok(**case.reply, has_more=False, response_metadata={"next_cursor": "dXNlcjpVMDYxTkZUVDI="})
    h = harness(*case.replies(final))
    result = _call(h, case)
    assert result.successful, result.error
    # Derived from the cursor, never from Slack's own has_more or the page size.
    assert (result.data["has_more"], result.data["next_cursor"]) == (True, "dXNlcjpVMDYxTkZUVDI=")


@pytest.mark.parametrize("case", PAGE_PAGED, ids=[c.id for c in PAGE_PAGED])
def test_page_hint(case: Case):
    paging = {"count": 20, "total": 45, "page": 1, "pages": 3}
    reply = {k: ({**v, "paging": paging} if isinstance(v, dict) else v) for k, v in case.reply.items()}
    h = harness(ok(**reply))
    result = _call(h, case)
    assert result.successful, result.error
    assert (result.data["has_more"], result.data["next_page"]) == (True, 2)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_not_found(case: Case):
    h = harness(slack_error("channel_not_found"))
    result = _call(h, case)
    assert result.successful is False
    assert result.error_code == "not_found"
    assert result.error.startswith(f"{case.methods[0]}: channel_not_found")
    assert "can't see it" in result.error
    assert result.data == {"slack_error": "channel_not_found", "status": 200}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_invalid_auth_says_reconnect(case: Case):
    h = harness(slack_error("invalid_auth"))
    result = _call(h, case)
    assert result.successful is False
    assert result.error_code == "unauthorized"
    assert "reconnect Slack" in result.error


RETRIED = [c for c in CASES if c.slug != "SLACK_API_TEST"]  # api.test never retries (echoes)


@pytest.mark.parametrize("case", RETRIED, ids=[c.id for c in RETRIED])
def test_rate_limit_is_retried(case: Case):
    limited = response(429, {"ok": False, "error": "ratelimited"}, headers={"Retry-After": "2"})
    h = harness(limited, *case.replies())
    result = _call(h, case)
    assert result.successful, result.error
    assert h.sleeps == [2.0]
    assert h.session.methods == [case.methods[0], *case.methods]


@pytest.mark.parametrize("case", CASES[:3], ids=IDS[:3])
def test_rate_limit_that_persists_reports_retry_after(case: Case):
    limited = [response(429, {"ok": False, "error": "ratelimited"}, headers={"Retry-After": "2"}) for _ in range(3)]
    h = harness(*limited)
    result = _call(h, case)
    assert result.successful is False
    assert result.error_code == "rate_limited"
    assert result.retry_after == 2.0
    assert "retry in about 2s" in result.error
    assert h.sleeps == [2.0, 2.0]
