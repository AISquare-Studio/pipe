"""Toolkits: a connector's catalog of LLM-callable tools plus its connection logic.

A :class:`Toolkit` hosts named tools (:class:`ToolSpec` — a slug, a
description and a JSON-Schema ``input_schema``) and executes them against a
``config`` carrying the user's credentials (``access_token`` plus any
connector-specific, non-secret keys). Its :class:`~aisquare.pipe.core.oauth.OAuthSpec`
describes how a user connects.

``call_tool`` is the single execution entry point and never raises: unknown
slugs, bad arguments, missing credentials and API failures all come back as
``ToolResult(successful=False, error=...)`` so an agent loop can show the
error to the model and carry on.
"""

from __future__ import annotations

import copy
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aisquare.pipe.core.connector import AuthType
from aisquare.pipe.core.envelope import DataEnvelope
from aisquare.pipe.core.oauth import (
    OAuthSpec,
    OAuthToken,
    build_authorize_url,
    exchange_code,
    refresh_access_token,
    revoke_token,
)
from aisquare.pipe.errors import OAuthError, ToolError

logger = logging.getLogger("aisquare.pipe")

#: Longest error message ``call_tool`` returns.
_MAX_ERROR = 1000


@dataclass(frozen=True)
class ToolSpec:
    """One tool in a toolkit's catalog.

    ``slug`` is UPPER_SNAKE and starts with the toolkit's ``slug_prefix``
    (``GITHUB_SEARCH_CODE``). ``input_schema`` is a JSON Schema object
    (``{"type": "object", "properties": {...}, "required": [...]}``).
    ``read_only`` tools never change remote state; ``destructive`` ones
    delete or overwrite.
    """

    slug: str
    description: str
    input_schema: Mapping[str, Any] = field(hash=False)
    read_only: bool
    destructive: bool = False
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))

    def to_llm_tool(self) -> dict[str, Any]:
        """The ``{name, description, input_schema}`` shape LLM tool-use APIs take."""
        return {
            "name": self.slug,
            "description": self.description,
            "input_schema": copy.deepcopy(dict(self.input_schema)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "description": self.description,
            "input_schema": copy.deepcopy(dict(self.input_schema)),
            "read_only": self.read_only,
            "destructive": self.destructive,
            "tags": list(self.tags),
        }


@dataclass
class ToolResult:
    """Outcome of one tool call.

    ``data`` is JSON-serializable. ``files`` carries binary payloads (e.g. a
    download) as DataEnvelopes — ``content_type`` is the MIME type and
    ``metadata["filename"]`` the file name — and is left out of ``to_dict``.
    """

    successful: bool
    data: Any = None
    error: str | None = None
    files: list[DataEnvelope] = field(default_factory=list)

    @classmethod
    def ok(cls, data: Any = None, files: list[DataEnvelope] | None = None) -> ToolResult:
        return cls(successful=True, data=data, files=list(files or []))

    @classmethod
    def fail(cls, error: str, data: Any = None) -> ToolResult:
        return cls(successful=False, data=data, error=error)

    def to_dict(self) -> dict[str, Any]:
        return {"successful": self.successful, "data": self.data, "error": self.error}


# --------------------------------------------------------------------------
# Ranking (ported from pipe-as-service's Composio keyword ranking)
# --------------------------------------------------------------------------

# Pronouns/articles/fillers dropped from the query so ranking keys on meaningful
# words. Verbs like "list"/"send"/"create" are kept — they match tool slugs.
STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "what", "are", "you",
        "your", "from", "my", "me", "of", "to", "in", "on", "up", "by", "do",
        "an", "is", "how", "many", "most", "their", "have", "has", "out",
        "any", "all",
    }
)

# First-person phrasing ("my repos", "issues assigned to me") points at a
# toolkit's authenticated-user variants over the "for a user / for a repo" ones.
_FIRST_PERSON = frozenset({"my", "me", "mine", "myself", "our", "ours", "i"})


def query_terms(text: str) -> set[str]:
    """Search terms for ``text``: words longer than two characters, minus
    stopwords, plus their plural-stripped forms, plus ``authenticated`` for
    first-person queries."""
    raw = re.split(r"[^a-z0-9]+", text.lower())
    words = {w for w in raw if len(w) > 2 and w not in STOPWORDS}
    terms = words | {w.rstrip("s") for w in words}
    if any(w in _FIRST_PERSON for w in raw):
        terms.add("authenticated")
    return terms


def rank_tools(
    tools: Sequence[ToolSpec], query: str | None, limit: int | None = 6
) -> list[ToolSpec]:
    """Deterministic keyword ranking: +2 per term found in the slug, +1 per
    term found in the description or tags. Ties keep catalog order. When no
    tool matches, the head of the catalog is returned."""
    cap = len(tools) if limit is None else max(limit, 0)
    if not query or not query.strip():
        return list(tools[:cap])
    terms = query_terms(query)

    def _score(tool: ToolSpec) -> int:
        slug = tool.slug.lower()
        text = " ".join((tool.description, *tool.tags)).lower()
        return sum(2 for t in terms if t in slug) + sum(1 for t in terms if t in text)

    scored = sorted(((_score(t), t) for t in tools), key=lambda pair: pair[0], reverse=True)
    matched = [t for score, t in scored if score > 0]
    chosen = matched or [t for _, t in scored]
    return chosen[:cap]


# --------------------------------------------------------------------------
# Argument validation (a deliberately small JSON Schema subset)
# --------------------------------------------------------------------------


def _type_ok(value: Any, json_type: str) -> bool:
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, (list, tuple))
    if json_type == "object":
        return isinstance(value, Mapping)
    if json_type == "null":
        return value is None
    return True  # unknown type keyword: do not block


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _schema_types(schema: Mapping[str, Any]) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [str(t) for t in declared]
    return []


def _label(path: str) -> str:
    return f"'{path}'" if path else "arguments"


def _validate(schema: Any, value: Any, path: str, problems: list[str]) -> None:
    if not isinstance(schema, Mapping):
        return
    types = _schema_types(schema)
    if types and not any(_type_ok(value, t) for t in types):
        problems.append(f"{_label(path)} must be {' or '.join(types)}, got {_json_type_name(value)}")
        return
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_same(value, e) for e in enum):
        allowed = ", ".join(repr(e) for e in enum)
        problems.append(f"{_label(path)} must be one of: {allowed}")
    if isinstance(value, Mapping):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"missing required argument {_label(_join(path, key))}")
        for key, sub_schema in properties.items():
            if key in value:
                _validate(sub_schema, value[key], _join(path, key), problems)
        if schema.get("additionalProperties") is False:
            unknown = sorted(str(k) for k in value if k not in properties)
            if unknown:
                problems.append(
                    f"unknown argument(s) {', '.join(repr(k) for k in unknown)}"
                    + (f" in {_label(path)}" if path else "")
                )
    elif isinstance(value, (list, tuple)):
        items = schema.get("items")
        for index, item in enumerate(value):
            _validate(items, item, f"{path}[{index}]", problems)


def _same(value: Any, allowed: Any) -> bool:
    # ``True == 1`` in Python; JSON keeps booleans and numbers apart.
    return value == allowed and isinstance(value, bool) == isinstance(allowed, bool)


def _join(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def validate_arguments(schema: Mapping[str, Any], arguments: Any) -> list[str]:
    """Check ``arguments`` against ``schema``; return human-readable problems
    (empty = valid). Covers ``type`` (incl. type lists), ``enum``,
    ``required``, ``properties``, ``items`` and ``additionalProperties:
    false``. Messages name keys and types, never argument values."""
    problems: list[str] = []
    if not isinstance(arguments, Mapping):
        return [f"arguments must be an object, got {_json_type_name(arguments)}"]
    _validate(schema, arguments, "", problems)
    return problems


def prepare_arguments(
    schema: Mapping[str, Any], arguments: Mapping[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """Normalize then validate top-level tool arguments.

    ``None`` for an optional argument whose schema does not allow ``null``
    is treated as absent (models often send explicit nulls), and top-level
    ``default`` values fill missing arguments. Returns ``(arguments,
    problems)``.
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        return {}, [f"arguments must be an object, got {_json_type_name(arguments)}"]
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = set(schema.get("required") or [])
    prepared: dict[str, Any] = {}
    for key, value in arguments.items():
        sub = properties.get(key)
        if (
            value is None
            and key not in required
            and not (isinstance(sub, Mapping) and "null" in _schema_types(sub))
        ):
            continue
        prepared[key] = value
    for key, sub in properties.items():
        if key not in prepared and isinstance(sub, Mapping) and "default" in sub:
            prepared[key] = copy.deepcopy(sub["default"])
    return prepared, validate_arguments(schema, prepared)


# --------------------------------------------------------------------------
# Toolkit
# --------------------------------------------------------------------------

_SECRET_CONFIG_KEYS = re.compile(r"token|secret|password|api_key|apikey|credential", re.IGNORECASE)


def default_slug_prefix(name: str) -> str:
    """``github`` -> ``GITHUB_``; ``one-drive`` -> ``ONE_DRIVE_``."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_"


class Toolkit(ABC):
    """Base class for a connector's tool catalog and connection logic.

    Subclasses set the class attributes, implement :meth:`tools` and
    :meth:`run_tool`, and register under the ``aisquare_pipe.toolkits``
    entry-point group with the entry-point name equal to ``name``.

    ``slug_prefix`` defaults to ``name.upper() + "_"``. The OAuth methods
    delegate to :mod:`aisquare.pipe.core.oauth` using ``oauth``; override
    them for provider quirks.
    """

    name: str
    version: str
    display_name: str = ""
    description: str = ""
    auth_type: AuthType = AuthType.OAUTH2
    slug_prefix: str = ""
    oauth: OAuthSpec | None = None
    docs_url: str = ""

    _auto_slug_prefix: bool = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("slug_prefix"):
            cls._auto_slug_prefix = False
        elif cls._auto_slug_prefix and isinstance(getattr(cls, "name", None), str):
            cls.slug_prefix = default_slug_prefix(cls.name)

    # -- catalog -----------------------------------------------------------

    @abstractmethod
    def tools(self) -> list[ToolSpec]:
        """Every tool this toolkit offers. Must not touch the network."""
        ...

    def get_tool(self, slug: str) -> ToolSpec | None:
        """The tool with this slug (exact, else case-insensitive), or None."""
        if not isinstance(slug, str):
            return None
        catalog = self.tools()
        for tool in catalog:
            if tool.slug == slug:
                return tool
        upper = slug.upper()
        for tool in catalog:
            if tool.slug == upper:
                return tool
        return None

    def search_tools(self, query: str, limit: int = 6) -> list[ToolSpec]:
        """Tools most relevant to a natural-language ``query`` (see :func:`rank_tools`)."""
        return rank_tools(self.tools(), query, limit)

    # -- execution -----------------------------------------------------------

    @abstractmethod
    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        """Execute ``tool``. Arguments are already validated against its
        schema (with defaults applied) and ``config`` passed
        :meth:`validate_config`. Raise :class:`~aisquare.pipe.errors.ToolError`
        (or return ``ToolResult.fail``) for expected failures; anything else
        raised is caught by :meth:`call_tool`."""
        ...

    def call_tool(
        self, slug: str, arguments: Mapping[str, Any] | None, config: Mapping[str, Any] | None
    ) -> ToolResult:
        """Run a tool by slug. Never raises — every failure is a
        ``ToolResult(successful=False)`` with a human-readable error."""
        started = time.monotonic()
        config = config if isinstance(config, Mapping) else {}
        try:
            result = self._call_tool(slug, arguments, config)
        except Exception as e:  # noqa: BLE001 — call_tool never raises
            result = ToolResult.fail(f"{slug}: internal error ({type(e).__name__})")
        if result.error:
            result.error = _scrub(result.error, config)
        logger.debug(
            "toolkit %s: %s successful=%s in %.0fms",
            getattr(self, "name", type(self).__name__),
            slug if isinstance(slug, str) else type(slug).__name__,
            result.successful,
            (time.monotonic() - started) * 1000,
        )
        return result

    def _call_tool(
        self, slug: str, arguments: Mapping[str, Any] | None, config: Mapping[str, Any]
    ) -> ToolResult:
        tool = self.get_tool(slug)
        if tool is None:
            shown = slug[:100] if isinstance(slug, str) else type(slug).__name__
            return ToolResult.fail(
                f"unknown tool {shown!r} for toolkit {self.name!r} — "
                "use search_tools() to find available tools"
            )
        prepared, problems = prepare_arguments(tool.input_schema, arguments)
        if problems:
            return ToolResult.fail(f"invalid arguments for {tool.slug}: " + "; ".join(problems))
        try:
            connected = self.validate_config(dict(config)) is True
        except Exception:  # noqa: BLE001 — a crashing check means "not connected"
            connected = False
        if not connected:
            return ToolResult.fail(
                f"{self.display_name or self.name} is not connected: "
                "config is missing valid credentials"
            )
        try:
            result = self.run_tool(tool, prepared, dict(config))
        except ToolError as e:
            return ToolResult.fail(str(e) or f"{tool.slug} failed")
        except Exception as e:  # noqa: BLE001 — surfaced to the caller, not raised
            logger.warning("toolkit %s: %s raised %s", self.name, tool.slug, type(e).__name__)
            return ToolResult.fail(f"{tool.slug} failed: {type(e).__name__}: {e}")
        if not isinstance(result, ToolResult):
            return ToolResult.fail(
                f"{tool.slug}: toolkit returned {type(result).__name__}, expected ToolResult"
            )
        if not result.successful and not result.error:
            result.error = f"{tool.slug} failed"
        return result

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Cheap, offline shape check (no network): True when ``config`` can
        authenticate. Default: no auth needed, or a non-empty ``access_token``."""
        if self.auth_type is AuthType.NONE:
            return True
        token = config.get("access_token") if isinstance(config, Mapping) else None
        return isinstance(token, str) and bool(token.strip())

    # -- connection (OAuth) ------------------------------------------------

    def _require_oauth(self) -> OAuthSpec:
        if self.oauth is None:
            raise OAuthError(
                f"toolkit {self.name!r} has no OAuth spec", error="oauth_not_supported"
            )
        return self.oauth

    def build_authorize_url(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        scopes: Sequence[str] | None = None,
        code_challenge: str | None = None,
    ) -> str:
        return build_authorize_url(
            self._require_oauth(),
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
            code_challenge=code_challenge,
        )

    def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> OAuthToken:
        return exchange_code(
            self._require_oauth(),
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )

    def refresh_access_token(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        refresh_token: str,
        scopes: Sequence[str] | None = None,
    ) -> OAuthToken:
        return refresh_access_token(
            self._require_oauth(),
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            scopes=scopes,
        )

    def revoke_token(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        token: str,
        token_type_hint: str = "access_token",
    ) -> bool:
        if self.oauth is None:
            return False
        return revoke_token(
            self.oauth,
            client_id=client_id,
            client_secret=client_secret,
            token=token,
            token_type_hint=token_type_hint,
        )


def _scrub(message: str, config: Mapping[str, Any]) -> str:
    """Mask any secret-looking config value that leaked into an error message."""
    for key, value in config.items():
        if isinstance(value, str) and len(value) >= 6 and _SECRET_CONFIG_KEYS.search(str(key)):
            message = message.replace(value, "***")
    return message[:_MAX_ERROR]
