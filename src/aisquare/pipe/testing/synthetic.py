"""Synthetic tool catalogues for search benchmarks and scale tests.

:func:`synthetic_catalog` makes connector cards and tool catalogues that read
like real ones: UPPER_SNAKE slugs built from varied verb/object vocabularies
(``ZORBIX_CRM_LIST_INVOICES_FOR_USER``), descriptions from several phrasings,
tags, and JSON-schema inputs with described properties. About a fifth of
the tools are near-duplicates of another (same verb and object, another
qualifier, an almost identical description), the distractors a real
catalogue has (``LIST_INVOICES`` / ``LIST_INVOICES_V2`` / ``SEARCH_INVOICES``),
so a benchmark does not run on easy, well-separated text.

Deterministic: the same arguments give the same catalogue, in any process.
No network, no randomness outside the ``seed``.
"""

from __future__ import annotations

import random
from typing import Any

from aisquare.pipe.core.tools import ToolSpec, default_slug_prefix

__all__ = ["synthetic_catalog"]

# Domain (card category) -> the objects its connectors manage.
_DOMAINS: dict[str, tuple[str, ...]] = {
    "crm": (
        "contact", "lead", "deal", "account", "opportunity", "pipeline", "stage", "quote",
        "invoice", "company", "note", "activity", "task", "campaign", "segment", "product",
    ),
    "project": (
        "issue", "ticket", "sprint", "epic", "board", "milestone", "comment", "label",
        "project", "subtask", "worklog", "release", "component", "attachment", "status",
    ),
    "files": (
        "file", "folder", "drive", "permission", "revision", "shortcut", "share link",
        "comment", "label", "trash item", "upload session", "thumbnail", "version",
    ),
    "chat": (
        "message", "channel", "thread", "reaction", "user", "group", "reminder", "emoji",
        "bookmark", "call", "huddle", "pin", "status", "workspace",
    ),
    "email": (
        "email", "thread", "draft", "label", "attachment", "filter", "alias", "signature",
        "contact", "mailbox", "folder", "rule",
    ),
    "calendar": (
        "event", "calendar", "attendee", "reminder", "room", "availability", "booking",
        "schedule", "holiday", "time slot", "recurrence",
    ),
    "code": (
        "repository", "branch", "commit", "pull request", "review", "release", "tag",
        "workflow run", "deployment", "environment", "secret", "webhook", "issue", "gist",
    ),
    "docs": (
        "document", "page", "space", "template", "comment", "revision", "block", "database",
        "section", "link", "attachment",
    ),
    "data": (
        "table", "row", "column", "dataset", "query", "dashboard", "report", "view", "schema",
        "job", "pipeline", "chart", "alert", "metric",
    ),
    "other": (
        "record", "item", "entity", "setting", "webhook", "audit log", "token", "member",
        "role", "team", "invoice", "order", "customer", "subscription", "payment", "refund",
    ),
}

# Verb -> (read_only, destructive, description phrasings). ``{o}`` is the
# object, ``{os}`` its plural, ``{c}`` the container, ``{f}`` a field.
_VERBS: dict[str, tuple[bool, bool, tuple[str, ...]]] = {
    "list": (True, False, (
        "List {os} in the {c}, newest first, one page at a time.",
        "Returns every {o} the connected user can see, optionally filtered by {f}.",
        "Page through the {os} of a {c}; pass next_cursor back for more.",
    )),
    "get": (True, False, (
        "Get one {o} by id: its {f}, owner and timestamps.",
        "Fetch a single {o} with all of its fields, including {f}.",
        "Read the details of a {o} (name, {f}, created and updated times).",
    )),
    "search": (True, False, (
        "Search {os} by keyword across the {c}; matches names, {f} and content.",
        "Full-text search over {os} with the provider's query syntax.",
        "Find {os} whose {f} or title contains the query.",
    )),
    "find": (True, False, (
        "Find a {o} by exact name or {f}; returns the first match or nothing.",
        "Look up {os} matching a filter on {f}.",
    )),
    "export": (True, False, (
        "Export {os} to CSV or JSON, with {f} as columns.",
        "Download a {o} as a file in the requested format.",
    )),
    "download": (True, False, (
        "Download the content of a {o}; the bytes come back as a file.",
        "Get the raw bytes of a {o} attached to the {c}.",
    )),
    "count": (True, False, (
        "Count the {os} in a {c}, optionally only those with {f}.",
    )),
    "preview": (True, False, (
        "Render a short preview of a {o}: title, {f} and a snippet.",
    )),
    "create": (False, False, (
        "Create a new {o} in the {c} with a name, {f} and optional notes.",
        "Add a {o}; returns its id and web link.",
        "Make a {o} from the given fields ({f} is required).",
    )),
    "update": (False, True, (
        "Update a {o}: only the fields given ({f}, name, notes) change.",
        "Edit an existing {o}'s {f} or description.",
        "Change a {o}; the new values replace the old.",
    )),
    "delete": (False, True, (
        "Delete a {o} for good. Cannot be undone.",
        "Permanently remove a {o} and everything attached to it.",
    )),
    "archive": (False, True, (
        "Archive a {o}: it leaves the {c} but can be restored.",
    )),
    "restore": (False, False, (
        "Restore an archived or trashed {o} to the {c}.",
    )),
    "send": (False, False, (
        "Send a {o} to one or more recipients, with {f} and optional attachments.",
        "Post a {o} to the {c} now.",
    )),
    "upload": (False, False, (
        "Upload a file as a new {o} in the {c} (base64 content, name, {f}).",
    )),
    "move": (False, False, (
        "Move a {o} to another {c}; its {f} is kept.",
    )),
    "copy": (False, False, (
        "Copy a {o}, optionally into another {c} and under a new name.",
    )),
    "rename": (False, False, (
        "Rename a {o}; everything else, including {f}, is kept.",
    )),
    "assign": (False, False, (
        "Assign a {o} to a user or team, replacing the current {f}.",
    )),
    "approve": (False, False, (
        "Approve a pending {o}; the requester is notified.",
    )),
    "close": (False, False, (
        "Close a {o}, with an optional reason and {f}.",
    )),
    "share": (False, False, (
        "Share a {o} with people or a group: grant view, comment or edit access.",
    )),
    "import": (False, False, (
        "Import {os} from a CSV file, mapping columns to {f}.",
    )),
    "sync": (False, False, (
        "Sync {os} with the source now instead of waiting for the next run.",
    )),
    "schedule": (False, False, (
        "Schedule a {o} for later: pass a time and {f}.",
    )),
    "cancel": (False, True, (
        "Cancel a scheduled or pending {o}; attendees or watchers are told.",
    )),
    "subscribe": (False, False, (
        "Subscribe to changes on a {o}; events arrive at a webhook.",
    )),
    "merge": (False, True, (
        "Merge two {os} into one; the second is deleted.",
    )),
}

# Verbs whose slugs name the object in the plural (LIST_INVOICES).
_PLURAL_VERBS = frozenset({"list", "search", "export", "count", "import", "sync"})

_QUALIFIERS = (
    "", "", "", "", "for_user", "by_id", "by_name", "in_folder", "for_the_authenticated_user",
    "in_bulk", "from_template", "v2", "with_filters", "for_team", "by_email", "for_project",
    "batch", "async", "details", "metadata",
)

_CONTAINERS = ("workspace", "project", "account", "folder", "team", "organization", "space")
_FIELDS = (
    "status", "owner", "due date", "priority", "tags", "assignee", "amount", "currency",
    "title", "description", "visibility", "region", "category", "external id", "color",
)
_PROPERTY_HELP = (
    "Only {os} with this {f}.",
    "The {f} to set.",
    "Sort by this field (default: created time).",
    "Maximum results per page (1-200, default 50).",
    "Cursor from the previous page's next_cursor.",
    "ISO 8601 timestamp; only {os} changed after it.",
    "Include archived {os} too.",
    "Comma-separated fields to return.",
)
_SYLLABLES = (
    "ac", "ze", "lo", "mi", "ra", "to", "vex", "nor", "qui", "bel", "dor", "fin", "gal",
    "har", "jun", "kel", "lum", "mar", "nex", "orb", "pax", "quo", "rin", "sol", "tav",
)
_FILLER = (
    "Needs the read scope.",
    "Respects the connection's permissions.",
    "Rate limited to 50 calls per minute.",
    "Returns has_more and next_cursor when there is more.",
    "Times are UTC.",
    "Ids are stable across renames.",
    "Archived items are left out unless asked for.",
    "",
    "",
)


def _plural(word: str) -> str:
    head, _, last = word.rpartition(" ")
    if last.endswith("y") and last[-2:-1] not in "aeiou":
        last = last[:-1] + "ies"
    elif last.endswith(("s", "x", "ch", "sh")):
        last += "es"
    else:
        last += "s"
    return f"{head} {last}".strip()


def _brand(rng: random.Random) -> str:
    return "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 3)))


def _tool(
    rng: random.Random,
    prefix: str,
    verb: str,
    obj: str,
    qualifier: str,
    container: str,
    *,
    phrasing: int | None = None,
) -> ToolSpec:
    read_only, destructive, phrasings = _VERBS[verb]
    field = rng.choice(_FIELDS)
    words = {"o": obj, "os": _plural(obj), "c": container, "f": field}
    pick = phrasings[phrasing % len(phrasings)] if phrasing is not None else rng.choice(phrasings)
    description = " ".join(
        part for part in (pick.format(**words), rng.choice(_FILLER), rng.choice(_FILLER)) if part
    )
    object_words = _plural(obj) if verb in _PLURAL_VERBS else obj
    slug = "_".join(
        part for part in (prefix.rstrip("_"), verb, object_words, qualifier) if part
    ).replace(" ", "_").upper()
    properties: dict[str, Any] = {}
    if verb in ("get", "update", "delete", "archive", "restore", "move", "copy", "rename", "assign",
                "approve", "close", "share", "cancel", "download", "preview", "merge", "subscribe"):
        properties[f"{obj.replace(' ', '_')}_id"] = {"type": "string", "description": f"The {obj}'s id."}
    if verb in ("search", "find"):
        properties["query"] = {"type": "string", "description": f"Words to match in {_plural(obj)}."}
    for _ in range(rng.randint(1, 6)):
        name = rng.choice(_FIELDS).replace(" ", "_")
        if rng.random() < 0.3:
            name = name.split("_")[0] + "".join(w.title() for w in name.split("_")[1:]) + "Filter"
        help_text = rng.choice(_PROPERTY_HELP).format(**{**words, "f": name.replace("_", " ")})
        properties.setdefault(name, {"type": "string", "description": help_text})
    if verb in ("list", "search", "export"):
        properties["page_size"] = {"type": "integer", "description": "Results per page."}
        properties["cursor"] = {"type": "string", "description": "Cursor from next_cursor."}
    required = [next(iter(properties))] if properties and verb not in ("list",) else []
    tags = [verb, obj, *rng.sample(_FIELDS, 2)]
    return ToolSpec(
        slug=slug,
        description=description,
        input_schema={"type": "object", "properties": properties, "required": required},
        read_only=read_only,
        destructive=destructive,
        tags=tuple(tags),
    )


def synthetic_catalog(
    n_connectors: int, tools_per_connector: int, *, seed: int = 0
) -> tuple[dict[str, dict[str, Any]], dict[str, list[ToolSpec]]]:
    """``n_connectors`` connector cards and ``tools_per_connector`` tools for
    each: ``(cards, tools)``, both keyed by connector name, in the same
    order. Cards have the :meth:`~aisquare.pipe.core.tools.Toolkit.card`
    keys; tool slugs start with the connector's
    :func:`~aisquare.pipe.core.tools.default_slug_prefix` and are unique
    within it. Deterministic for a given ``seed``."""
    rng = random.Random(seed)
    domains = sorted(_DOMAINS)
    all_objects = sorted({obj for objs in _DOMAINS.values() for obj in objs})
    cards: dict[str, dict[str, Any]] = {}
    tools: dict[str, list[ToolSpec]] = {}
    for i in range(n_connectors):
        domain = domains[i % len(domains)] if i < len(domains) else rng.choice(domains)
        brand = _brand(rng)
        name = f"{brand}_{domain}" if n_connectors <= 1 else f"{brand}_{domain}_{i}"
        display = f"{brand.title()} {domain.upper() if len(domain) <= 3 else domain.title()}"
        prefix = default_slug_prefix(name)
        own = list(_DOMAINS[domain])
        # Big catalogues borrow other domains' objects, as real APIs grow.
        pairs_per_object = len(_VERBS) * 3
        extra = max(0, -(-tools_per_connector // pairs_per_object) - len(own))
        objects = own + rng.sample([o for o in all_objects if o not in own], min(extra, len(all_objects) - len(own)))
        container = rng.choice(_CONTAINERS)
        catalogue: list[ToolSpec] = []
        made: list[tuple[str, str]] = []  # (verb, object) of each tool
        seen: set[str] = set()
        verbs = sorted(_VERBS)
        attempts = 0
        while len(catalogue) < tools_per_connector:
            attempts += 1
            if catalogue and rng.random() < 0.2:
                # A near-duplicate of an existing tool: same verb and object,
                # another qualifier, almost the same description.
                verb, obj = rng.choice(made)
                spec = _tool(rng, prefix, verb, obj, rng.choice(_QUALIFIERS[4:]), container, phrasing=0)
            else:
                verb = rng.choice(verbs)
                obj = rng.choice(objects)
                spec = _tool(rng, prefix, verb, obj, rng.choice(_QUALIFIERS), container)
            if spec.slug in seen:
                if attempts < tools_per_connector * 20:
                    continue
                spec = ToolSpec(
                    slug=f"{spec.slug}_{len(catalogue)}",
                    description=spec.description,
                    input_schema=spec.input_schema,
                    read_only=spec.read_only,
                    destructive=spec.destructive,
                    tags=spec.tags,
                )
            seen.add(spec.slug)
            catalogue.append(spec)
            made.append((verb, obj))
        keywords = [domain, *rng.sample(own, min(8, len(own)))]
        cards[name] = {
            "name": name,
            "display_name": display,
            "description": (
                f"{display}: {', '.join(_plural(o) for o in own[:6])} and more; "
                f"list, search, create, update and delete them."
            ),
            "category": domain,
            "keywords": keywords,
            "examples": [
                f"find the {own[0]} for Acme",
                f"create a {own[1 % len(own)]} for the Q3 launch",
                f"what changed in my {_plural(own[2 % len(own)])} this week",
            ],
            "auth_type": "oauth2",
        }
        tools[name] = catalogue
    return cards, tools
