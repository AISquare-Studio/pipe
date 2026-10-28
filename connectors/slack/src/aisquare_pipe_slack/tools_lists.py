"""Slack Lists tools (``slackLists.*``): lists, their items (rows), and who
may see them. Lists exist only on paid workspaces; elsewhere Slack answers
``paid_only`` / ``lists_disabled_user_team`` (``forbidden``).

A List's ID is a file ID (``F…``), an item's a record ID (``Rec…``), a
column's ``Col…``. Text cells and list descriptions are Block Kit
``rich_text``: a plain string is wrapped into one here, as Composio does, so
a model can write ``{"column_id": "Col…", "text": "Ship it"}``.

Every ``slackLists.*`` call goes as JSON (Slack recommends it for their
structured arguments). Tiers: reads are ``read``; creating and changing
lists, items and access are ``write``; deleting items and removing access
are ``destructive``. Changing a cell overwrites its value, so
SLACK_UPDATE_SLACK_LIST_ITEM is flagged destructive like a message edit.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from aisquare.pipe import ToolResult

from aisquare_pipe_slack.constants import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE
from aisquare_pipe_slack.tools import (
    CURSOR,
    Handler,
    SlackCall,
    SlackTool,
    _boolean,
    _clamp,
    _cursor_page,
    _invalid,
    _payload,
    _schema,
    _string,
    _tool,
)

AREA = "lists"
READ_SCOPES = ("lists:read",)
WRITE_SCOPES = ("lists:write",)
#: slackLists.items.list page size (default, max).
ITEMS_DEFAULT_LIMIT = 50
ITEMS_MAX_LIMIT = 200
#: slackLists.items.update: at most this many cells per call (Slack's documented maximum).
MAX_CELLS = 100

LIST_ID = _string("The List's ID (F…), as SLACK_CREATE_SLACK_LIST returned it.")
ITEM_ID = _string("The item's (row's) ID (Rec…).")
ACCESS_TARGETS = "Pass exactly one of user_ids or channel_ids."


def _array_or_json(description: str) -> dict[str, Any]:
    return {"type": ["array", "string"], "items": {"type": "object"}, "description": description}


def _ids_schema(description: str) -> dict[str, Any]:
    return {"type": ["array", "string"], "items": {"type": "string"}, "description": description}


DESCRIPTION_BLOCKS = {
    "type": ["string", "array", "object"],
    "description": "The description: plain text (wrapped into a rich_text block), or Block Kit rich_text "
    "block(s).",
}
CELLS_HELP = (
    "Each cell is {column_id, <typed value>}: rich_text (or plain 'text', wrapped for you), select "
    "(an array of choice values), user (an array of user IDs), date (['YYYY-MM-DD']), number, "
    "checkbox, ..."
)


# --------------------------------------------------------------------------
# Argument shaping
# --------------------------------------------------------------------------


def rich_text(text: str) -> dict[str, Any]:
    """A Block Kit ``rich_text`` block holding ``text`` as plain text."""
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}],
    }


def _json_value(value: Any, name: str) -> Any:
    """``value``, decoded when it is a JSON array or object in a string."""
    if isinstance(value, str) and value.lstrip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            raise _invalid(f"{name} is not valid JSON") from None
    return value


def _blocks_or_none(value: Any) -> list[dict[str, Any]] | None:
    """``value`` as a list of Block Kit blocks (one block is wrapped), or
    None when it isn't one."""
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list) and value and all(isinstance(b, dict) and b.get("type") for b in value):
        return value
    return None


def _description_blocks(value: Any) -> list[dict[str, Any]]:
    """Blocks for ``description_blocks``: a list of blocks as is, one block
    wrapped in a list, plain text wrapped into a ``rich_text`` block. A
    string is blocks only when it is their JSON; anything else in it — even
    '[Draft] Q3 plan' — is plain text."""
    if isinstance(value, str):
        if not value.strip():
            raise _invalid("description_blocks is empty")
        if value.lstrip()[:1] in ("[", "{"):
            try:
                blocks = _blocks_or_none(json.loads(value))
            except (ValueError, RecursionError):
                blocks = None
            if blocks is not None:
                return blocks
        return [rich_text(value)]
    blocks = _blocks_or_none(value)
    if blocks is None:
        raise _invalid("description_blocks must be text, or Block Kit blocks with a type")
    return blocks


def _objects(value: Any, name: str) -> list[dict[str, Any]]:
    decoded = _json_value(value, name)
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise _invalid(f"{name} must be an array of objects")
    return [dict(item) for item in decoded]


def _cells(value: Any, name: str, *, row_ids: bool) -> list[dict[str, Any]]:
    """Cells with plain ``text`` wrapped into ``rich_text``; each needs a
    ``column_id`` (and, for updates, a ``row_id``)."""
    cells = _objects(value, name)
    if not cells:
        raise _invalid(f"{name} is empty")
    if row_ids and len(cells) > MAX_CELLS:  # items.update's documented cap
        raise _invalid(f"{name}: at most {MAX_CELLS} cells per call")
    for cell in cells:
        if not isinstance(cell.get("column_id"), str) or not cell["column_id"]:
            raise _invalid(f"every cell in {name} needs a column_id")
        if row_ids and (not isinstance(cell.get("row_id"), str) or not cell["row_id"]):
            raise _invalid(f"every cell in {name} needs the row_id of an existing item")
        text = cell.get("text")
        if isinstance(text, str) and "rich_text" not in cell:
            del cell["text"]
            cell["rich_text"] = [rich_text(text)]
    return cells


def _id_array(value: Any, name: str) -> list[str]:
    decoded = _json_value(value, name)
    items = decoded if isinstance(decoded, list) else str(decoded).split(",")
    ids = [str(item).strip() for item in items if str(item).strip()]
    if not ids:
        raise _invalid(f"{name} must name at least one ID")
    return ids


def _one_target(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    """Exactly one of ``user_ids`` / ``channel_ids``, as arrays."""
    given = [n for n in ("user_ids", "channel_ids") if arguments.get(n) not in (None, "", [])]
    if len(given) != 1:
        raise _invalid(ACCESS_TARGETS)
    body[given[0]] = _id_array(arguments[given[0]], given[0])


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

Prepare = Callable[[dict[str, Any], dict[str, Any]], None]


def _lists_call(
    method: str,
    names: Iterable[str],
    prepare: Prepare | None = None,
    *,
    paged: bool = False,
) -> Handler:
    """One JSON ``slackLists.*`` call with ``names`` passed through;
    ``prepare(arguments, body)`` reshapes the body in place first."""
    passed = tuple(names)

    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        body = {n: arguments[n] for n in passed if arguments.get(n) is not None}
        if prepare is not None:
            prepare(arguments, body)
        payload = call.call(method, body, json_body=True)
        return ToolResult.ok(_cursor_page(payload) if paged else _payload(payload))

    return handler


def _prepare_create(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    if "description_blocks" in body:
        body["description_blocks"] = _description_blocks(body["description_blocks"])
    if "schema" in body:
        body["schema"] = _objects(body["schema"], "schema")


def _prepare_update(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    if not any(arguments.get(n) is not None for n in ("name", "description_blocks", "todo_mode")):
        raise _invalid("nothing to change: pass name, description_blocks or todo_mode")
    if "description_blocks" in body:
        body["description_blocks"] = _description_blocks(body["description_blocks"])


def _prepare_item(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    if "initial_fields" in body:
        body["initial_fields"] = _cells(body["initial_fields"], "initial_fields", row_ids=False)


def _prepare_cells(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    body["cells"] = _cells(body.get("cells"), "cells", row_ids=True)


def _prepare_items_page(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    body["limit"] = _clamp(body.get("limit"), ITEMS_DEFAULT_LIMIT, ITEMS_MAX_LIMIT)


def _prepare_ids(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    body["ids"] = _id_array(body.get("ids"), "ids")


def _prepare_access_set(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    _one_target(arguments, body)
    if body.get("access_level") == "owner" and "user_ids" not in body:
        raise _invalid("access_level 'owner' can only be granted to users (user_ids)")


def _prepare_access_delete(arguments: dict[str, Any], body: dict[str, Any]) -> None:
    _one_target(arguments, body)


# --------------------------------------------------------------------------
# Catalog rows
# --------------------------------------------------------------------------

LISTS_CATALOG: tuple[SlackTool, ...] = (
    _tool(
        "SLACK_CREATE_SLACK_LIST", TIER_WRITE, AREA, "slackLists.create",
        "Create a Slack List (paid workspaces): a name, optionally a description_blocks description "
        "and a schema of typed columns; or copy another List's columns (copy_from_list_id, and its "
        "rows with include_copied_list_records); todo_mode adds task columns. Returns list_id.",
        _schema(
            {
                "name": _string("The List's title, e.g. 'Sprint tasks'."),
                "description_blocks": DESCRIPTION_BLOCKS,
                "schema": _array_or_json(
                    "Column definitions: objects with name, type (text, select, user, date, number, "
                    "checkbox, ...) and type options such as select choices. Omit for Slack's defaults."
                ),
                "copy_from_list_id": _string("Copy the columns of this List (F…)."),
                "include_copied_list_records": _boolean("With copy_from_list_id: also copy its items."),
                "todo_mode": _boolean("Add Slack's task columns (assignee, due date, completed)."),
            },
            ["name"],
        ),
        _lists_call(
            "slackLists.create",
            ("name", "description_blocks", "schema", "copy_from_list_id", "include_copied_list_records",
             "todo_mode"),
            _prepare_create,
        ),
        user=WRITE_SCOPES,
        bot=WRITE_SCOPES,
        tags=("list", "lists", "create", "new", "tracker", "table", "tasks"),
    ),
    _tool(
        "SLACK_UPDATE_SLACK_LIST", TIER_WRITE, AREA, "slackLists.update",
        "Rename a Slack List, change its description, or turn todo_mode on or off. The List is "
        "named by id (not list_id).",
        _schema(
            {
                "id": LIST_ID,
                "name": _string("The new title."),
                "description_blocks": DESCRIPTION_BLOCKS,
                "todo_mode": _boolean("Turn task columns on (true) or off (false)."),
            },
            ["id"],
        ),
        _lists_call("slackLists.update", ("id", "name", "description_blocks", "todo_mode"), _prepare_update),
        user=WRITE_SCOPES,
        bot=WRITE_SCOPES,
        tags=("list", "lists", "rename", "update", "description"),
    ),
    _tool(
        "SLACK_CREATE_SLACK_LIST_ITEM", TIER_WRITE, AREA, "slackLists.items.create",
        "Add an item (row) to a Slack List, with initial_fields as its cell values; or copy an item "
        "(duplicated_item_id), or add a subtask (parent_item_id). " + CELLS_HELP,
        _schema(
            {
                "list_id": LIST_ID,
                "initial_fields": _array_or_json("Cell values for the new item. " + CELLS_HELP),
                "parent_item_id": _string("Make the item a subtask of this item (Rec…)."),
                "duplicated_item_id": _string("Copy this item (Rec…); initial_fields override its values."),
            },
            ["list_id"],
        ),
        _lists_call(
            "slackLists.items.create",
            ("list_id", "initial_fields", "parent_item_id", "duplicated_item_id"),
            _prepare_item,
        ),
        user=WRITE_SCOPES,
        bot=WRITE_SCOPES,
        tags=("list", "item", "row", "add", "create", "task", "record"),
    ),
    _tool(
        "SLACK_GET_SLACK_LIST_ITEM", TIER_READ, AREA, "slackLists.items.info",
        "One item (row) of a Slack List with its cell values (by column_id) and the List's details.",
        _schema(
            {
                "list_id": LIST_ID,
                "id": ITEM_ID,
                "include_is_subscribed": _boolean("Also say whether the caller follows the item."),
            },
            ["list_id", "id"],
        ),
        _lists_call("slackLists.items.info", ("list_id", "id", "include_is_subscribed")),
        user=READ_SCOPES,
        bot=READ_SCOPES,
        tags=("list", "item", "row", "get", "read", "record", "task"),
    ),
    _tool(
        "SLACK_LIST_SLACK_LIST_ITEMS", TIER_READ, AREA, "slackLists.items.list",
        "The items (rows) of a Slack List with their cell values, one page at a time "
        f"(default {ITEMS_DEFAULT_LIMIT}, max {ITEMS_MAX_LIMIT}); archived=true lists archived items "
        "instead. Pass next_cursor back as cursor for more.",
        _schema(
            {
                "list_id": LIST_ID,
                "limit": {
                    "type": "integer",
                    "description": f"Items per page (default {ITEMS_DEFAULT_LIMIT}, max {ITEMS_MAX_LIMIT}).",
                    "default": ITEMS_DEFAULT_LIMIT,
                },
                "cursor": CURSOR,
                "archived": _boolean("List archived items instead of active ones."),
            },
            ["list_id"],
        ),
        _lists_call(
            "slackLists.items.list", ("list_id", "limit", "cursor", "archived"), _prepare_items_page, paged=True
        ),
        user=READ_SCOPES,
        bot=READ_SCOPES,
        tags=("list", "items", "rows", "read", "tasks", "records", "table"),
    ),
    _tool(
        "SLACK_UPDATE_SLACK_LIST_ITEM", TIER_WRITE, AREA, "slackLists.items.update",
        "Change cells of existing Slack List items: each cell names its row_id (Rec…) and column_id "
        f"and carries the new value, which replaces the old one (at most {MAX_CELLS} cells). To add "
        "an item use SLACK_CREATE_SLACK_LIST_ITEM. " + CELLS_HELP,
        _schema(
            {
                "list_id": LIST_ID,
                "cells": _array_or_json("The cells to set, each with row_id and column_id. " + CELLS_HELP),
            },
            ["list_id", "cells"],
        ),
        _lists_call("slackLists.items.update", ("list_id", "cells"), _prepare_cells),
        user=WRITE_SCOPES,
        bot=WRITE_SCOPES,
        tags=("list", "item", "row", "update", "edit", "cell", "status", "task"),
        overwrites=True,
    ),
    _tool(
        "SLACK_DELETE_SLACK_LIST_ITEM", TIER_DESTRUCTIVE, AREA, "slackLists.items.delete",
        "Delete one item (row) from a Slack List. Cannot be undone.",
        _schema({"list_id": LIST_ID, "id": ITEM_ID}, ["list_id", "id"]),
        _lists_call("slackLists.items.delete", ("list_id", "id")),
        user=WRITE_SCOPES,
        bot=WRITE_SCOPES,
        tags=("list", "item", "row", "delete", "remove"),
    ),
    _tool(
        "SLACK_DELETE_MULTIPLE_SLACK_LIST_ITEMS", TIER_DESTRUCTIVE, AREA, "slackLists.items.deleteMultiple",
        "Delete several items (rows) of one Slack List at once. Cannot be undone. Slack answers "
        "for the whole batch, with no per-item result: after a failure, list the items to see "
        "which remain before retrying.",
        _schema(
            {"list_id": LIST_ID, "ids": _ids_schema("Item IDs (Rec…): an array or a comma-separated string.")},
            ["list_id", "ids"],
        ),
        _lists_call("slackLists.items.deleteMultiple", ("list_id", "ids"), _prepare_ids),
        user=WRITE_SCOPES,
        bot=WRITE_SCOPES,
        tags=("list", "items", "rows", "delete", "bulk", "remove"),
    ),
    _tool(
        "SLACK_SET_SLACK_LIST_ACCESS", TIER_WRITE, AREA, "slackLists.access.set",
        "Share a Slack List: give users or channels read, write or owner access (owner: users "
        "only). " + ACCESS_TARGETS,
        _schema(
            {
                "list_id": LIST_ID,
                "access_level": _string("The access to grant.", enum=("read", "write", "owner")),
                "user_ids": _ids_schema("User IDs to grant access to."),
                "channel_ids": _ids_schema("Channel IDs to grant access to."),
            },
            ["list_id", "access_level"],
        ),
        _lists_call("slackLists.access.set", ("list_id", "access_level"), _prepare_access_set),
        user=WRITE_SCOPES,
        bot=WRITE_SCOPES,
        tags=("list", "share", "access", "permission", "grant"),
    ),
    _tool(
        "SLACK_DELETE_SLACK_LIST_ACCESS", TIER_DESTRUCTIVE, AREA, "slackLists.access.delete",
        "Take away users' or channels' access to a Slack List. " + ACCESS_TARGETS,
        _schema(
            {
                "list_id": LIST_ID,
                "user_ids": _ids_schema("User IDs whose access to remove."),
                "channel_ids": _ids_schema("Channel IDs whose access to remove."),
            },
            ["list_id"],
        ),
        _lists_call("slackLists.access.delete", ("list_id",), _prepare_access_delete),
        user=WRITE_SCOPES,
        bot=WRITE_SCOPES,
        tags=("list", "unshare", "access", "permission", "revoke", "remove"),
    ),
)

__all__ = ["LISTS_CATALOG", "rich_text"]
