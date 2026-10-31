"""Pure, credential-free contract checks for connectors (Layer A).

Every check is a plain function returning ``list[CheckResult]`` so the same
logic backs three consumers: the per-connector compliance suite
(:mod:`aisquare.pipe.testing.compliance`), the root contract test
(``tests/test_connector_contracts.py``), and ``pipe validate``.

Behavioral checks run under :func:`no_network` — a best-effort socket guard
that blocks INET/INET6 sockets, ``create_connection`` and DNS resolution.
Networking that bypasses the ``socket`` module (C-level stacks) escapes the
guard; no current connector uses one.
"""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from importlib.metadata import entry_points
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PushResult
from aisquare.pipe.core.oauth import FLOW_AUTHORIZE_PARAMS, OAuthSpec
from aisquare.pipe.core.registry import slug_prefix_conflicts
from aisquare.pipe.core.tools import (
    CARD_FIELDS,
    CATEGORIES,
    ERROR_CODES,
    RISKS,
    Toolkit,
    ToolResult,
    ToolSpec,
)
from aisquare.pipe.errors import PipeError
from aisquare.pipe.testing.fixtures import (
    make_binary_envelope,
    make_json_envelope,
    make_text_envelope,
)

#: Exception types a connector may legitimately raise when poked with empty
#: config/params. Anything else (TypeError, KeyError, AttributeError, ...)
#: indicates a crash path rather than a designed error.
CLEAN_ERRORS = (PipeError, ValueError)

ENTRY_POINT_GROUP = "aisquare_pipe.connectors"
TOOLKIT_ENTRY_POINT_GROUP = "aisquare_pipe.toolkits"


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class CheckResult:
    """Outcome of one validation rule."""

    id: str
    passed: bool
    severity: Severity = Severity.ERROR
    message: str = ""


def failures(results: list[CheckResult]) -> list[CheckResult]:
    """Error-severity failures from a result list."""
    return [r for r in results if not r.passed and r.severity is Severity.ERROR]


class NetworkBlockedError(RuntimeError):
    """Raised when code under ``no_network()`` attempts network access."""


@dataclass
class NetworkLog:
    """Attempts recorded by the guard — populated even when the raised
    NetworkBlockedError is swallowed by the code under test."""

    attempts: list[str] = field(default_factory=list)


_BLOCKED_FAMILIES = {socket.AF_INET, socket.AF_INET6}


@contextmanager
def no_network() -> Iterator[NetworkLog]:
    """Block INET socket creation, ``create_connection`` and DNS lookups.

    Records each attempt in the yielded :class:`NetworkLog` *and then*
    raises :class:`NetworkBlockedError` — the record survives even when the
    caller catches the exception (e.g. a sink's broad ``except Exception``).

    AF_UNIX sockets and fd-wrapping stay allowed (asyncio/multiprocessing
    internals). Patches module-global state: not thread-safe; use
    sequentially only.
    """
    log = NetworkLog()
    real_socket = socket.socket
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    class _GuardedSocket(real_socket):  # type: ignore[misc,valid-type]
        def __init__(
            self,
            family: int = -1,
            type: int = -1,
            proto: int = -1,
            fileno: Any = None,
        ) -> None:
            resolved = socket.AF_INET if family == -1 else family
            if fileno is None and resolved in _BLOCKED_FAMILIES:
                log.attempts.append(f"socket.socket(family={resolved!r})")
                raise NetworkBlockedError(
                    "network access blocked: socket.socket() for an INET family"
                )
            super().__init__(family, type, proto, fileno)

    def _blocked_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        log.attempts.append(f"socket.create_connection({address!r})")
        raise NetworkBlockedError(
            f"network access blocked: create_connection({address!r})"
        )

    def _blocked_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        log.attempts.append(f"socket.getaddrinfo({host!r}, {port!r})")
        raise NetworkBlockedError(
            f"network access blocked: getaddrinfo({host!r}, {port!r})"
        )

    socket.socket = _GuardedSocket  # type: ignore[misc]
    socket.create_connection = _blocked_create_connection  # type: ignore[assignment]
    socket.getaddrinfo = _blocked_getaddrinfo  # type: ignore[assignment]
    try:
        yield log
    finally:
        socket.socket = real_socket  # type: ignore[misc]
        socket.create_connection = real_create_connection  # type: ignore[assignment]
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Entry-point discovery (with metadata the registry hides)
# --------------------------------------------------------------------------


@dataclass
class DiscoveredConnector:
    """One ``aisquare_pipe.connectors`` (or ``aisquare_pipe.toolkits``)
    entry point, load failures included."""

    ep_name: str
    ep_value: str
    dist_name: str | None
    dist_version: str | None
    cls: type | None
    load_error: str | None
    group: str = ENTRY_POINT_GROUP

    @property
    def is_toolkit(self) -> bool:
        return self.group == TOOLKIT_ENTRY_POINT_GROUP


def _discover_entry_points(group: str) -> list[DiscoveredConnector]:
    discovered: list[DiscoveredConnector] = []
    for ep in entry_points(group=group):
        dist = getattr(ep, "dist", None)
        cls: type | None = None
        load_error: str | None = None
        try:
            cls = ep.load()
        except Exception as e:  # noqa: BLE001 — any load failure is the finding
            load_error = f"{type(e).__name__}: {e}"
        discovered.append(
            DiscoveredConnector(
                ep_name=ep.name,
                ep_value=ep.value,
                dist_name=getattr(dist, "name", None),
                dist_version=getattr(dist, "version", None),
                cls=cls,
                load_error=load_error,
                group=group,
            )
        )
    return sorted(discovered, key=lambda d: d.ep_name)


def discover_connector_entry_points() -> list[DiscoveredConnector]:
    """All connector entry points with dist identity and load outcome.

    Unlike :func:`aisquare.pipe.core.registry.discover_connectors`, load
    failures are returned (not just logged) and the owning distribution is
    reported — both are needed to attribute problems to a connector package.
    """
    return _discover_entry_points(ENTRY_POINT_GROUP)


def discover_toolkit_entry_points() -> list[DiscoveredConnector]:
    """All ``aisquare_pipe.toolkits`` entry points (see
    :func:`discover_connector_entry_points`)."""
    return _discover_entry_points(TOOLKIT_ENTRY_POINT_GROUP)


def discover_all_entry_points() -> list[DiscoveredConnector]:
    """Connector and toolkit entry points together."""
    return discover_connector_entry_points() + discover_toolkit_entry_points()


# --------------------------------------------------------------------------
# Declaration checks
# --------------------------------------------------------------------------


def _is_valid_semver(version: str) -> bool:
    import re

    return bool(re.match(r"^\d+\.\d+\.\d+", version))


def _is_valid_mime(mime: str) -> bool:
    return "/" in mime


def check_instantiation(cls: type) -> list[CheckResult]:
    """Zero-arg construction must succeed without touching the network."""
    cid = "contract.instantiate"
    try:
        with no_network() as log:
            cls()
    except Exception as e:  # noqa: BLE001
        return [CheckResult(cid, False, message=f"{cls.__name__}() raised {type(e).__name__}: {e}")]
    if log.attempts:
        return [CheckResult(cid, False, message=f"network during __init__: {log.attempts[0]}")]
    return [CheckResult(cid, True)]


def check_name(cls: type) -> list[CheckResult]:
    inst = cls()
    ok = isinstance(inst.name, str) and len(inst.name) > 0
    return [CheckResult("contract.name", ok, message="" if ok else "name must be a non-empty str")]


def check_version(cls: type) -> list[CheckResult]:
    inst = cls()
    ok = isinstance(inst.version, str) and _is_valid_semver(inst.version)
    return [
        CheckResult(
            "contract.version-semver",
            ok,
            message="" if ok else f"version {getattr(inst, 'version', None)!r} is not valid semver",
        )
    ]


def check_auth_type(cls: type) -> list[CheckResult]:
    inst = cls()
    ok = isinstance(inst.auth_type, AuthType)
    return [
        CheckResult(
            "contract.auth-type", ok, message="" if ok else "auth_type must be an AuthType member"
        )
    ]


def check_metadata_spec(cls: type) -> list[CheckResult]:
    inst = cls()
    spec = getattr(inst, "metadata_spec", {}) or {}
    bad = [k for k, v in spec.items() if not isinstance(v, MetaField)]
    ok = not bad
    return [
        CheckResult(
            "contract.metadata-spec",
            ok,
            message="" if ok else f"metadata_spec values must be MetaField: {', '.join(bad)}",
        )
    ]


def check_entry_point_name(cls: type, ep_name: str) -> list[CheckResult]:
    """Entry-point name must equal the connector name or extend it with a
    ``-`` suffix (house convention: ``n8n`` ↔ ``n8n-source``)."""
    inst = cls()
    ok = ep_name == inst.name or ep_name.startswith(inst.name + "-")
    return [
        CheckResult(
            "contract.entry-point-name",
            ok,
            message=""
            if ok
            else f"entry point {ep_name!r} does not match connector name {inst.name!r}",
        )
    ]


def check_output_types(cls: type) -> list[CheckResult]:
    inst = cls()
    types = getattr(inst, "output_types", None)
    ok = (
        isinstance(types, list)
        and len(types) > 0
        and all(isinstance(t, str) and _is_valid_mime(t) for t in types)
    )
    return [
        CheckResult(
            "contract.source.output-types",
            ok,
            message="" if ok else f"output_types must be a non-empty list of MIME strings, got {types!r}",
        )
    ]


def check_input_types(cls: type) -> list[CheckResult]:
    inst = cls()
    types = getattr(inst, "input_types", None)
    ok = (
        isinstance(types, list)
        and len(types) > 0
        and all(isinstance(t, str) and _is_valid_mime(t) for t in types)
    )
    return [
        CheckResult(
            "contract.sink.input-types",
            ok,
            message="" if ok else f"input_types must be a non-empty list of MIME strings, got {types!r}",
        )
    ]


# --------------------------------------------------------------------------
# Behavioral no-creds checks
# --------------------------------------------------------------------------


def _validate_config_check(cls: type, check_id: str) -> list[CheckResult]:
    inst = cls()
    try:
        with no_network() as log:
            result = inst.validate_config({})
    except Exception as e:  # noqa: BLE001
        return [
            CheckResult(
                check_id,
                False,
                message=f"validate_config({{}}) raised {type(e).__name__}: {e} — must return a bool",
            )
        ]
    if log.attempts:
        return [CheckResult(check_id, False, message=f"network in validate_config({{}}): {log.attempts[0]}")]
    if not isinstance(result, bool):
        return [CheckResult(check_id, False, message=f"validate_config({{}}) returned {type(result).__name__}, expected bool")]
    if inst.auth_type is not AuthType.NONE and result is not False:
        return [
            CheckResult(
                check_id,
                False,
                message="validate_config({}) returned True for an authenticated connector — empty config cannot hold credentials",
            )
        ]
    return [CheckResult(check_id, True)]


def check_source_validate_config(cls: type) -> list[CheckResult]:
    return _validate_config_check(cls, "contract.source.validate-config-no-creds")


def check_sink_validate_config(cls: type) -> list[CheckResult]:
    return _validate_config_check(cls, "contract.sink.validate-config-no-creds")


def check_pull_contract(cls: type) -> list[CheckResult]:
    """``pull({})`` must return an iterator whose first ``next()`` either
    yields, stops, or raises a designed error — never a crash type or a
    network attempt."""
    inst = cls()
    iter_id = "contract.source.pull-iterator"
    creds_id = "contract.source.pull-no-creds"

    try:
        with no_network() as log:
            result = inst.pull({})
    except CLEAN_ERRORS:
        # Eagerly-raising (non-generator) pull is tolerated when the error is clean.
        return [CheckResult(iter_id, True), CheckResult(creds_id, True)]
    except Exception as e:  # noqa: BLE001
        return [
            CheckResult(iter_id, True),
            CheckResult(creds_id, False, message=f"pull({{}}) raised {type(e).__name__}: {e} — expected a PipeError/ValueError"),
        ]
    if log.attempts:
        return [
            CheckResult(iter_id, True),
            CheckResult(creds_id, False, message=f"network in pull({{}}): {log.attempts[0]}"),
        ]

    if not (isinstance(result, Iterator) or hasattr(result, "__next__")):
        return [
            CheckResult(iter_id, False, message=f"pull({{}}) returned {type(result).__name__}, expected an iterator/generator"),
            CheckResult(creds_id, False, message="not evaluated — pull({}) did not return an iterator"),
        ]

    try:
        with no_network() as log:
            next(result)
    except StopIteration:
        pass
    except NetworkBlockedError:
        return [
            CheckResult(iter_id, True),
            CheckResult(creds_id, False, message=f"network on first next(pull({{}})): {log.attempts[0]}"),
        ]
    except CLEAN_ERRORS:
        pass
    except Exception as e:  # noqa: BLE001
        return [
            CheckResult(iter_id, True),
            CheckResult(
                creds_id,
                False,
                message=f"first next(pull({{}})) raised {type(e).__name__}: {e} — expected ConfigValidationError/PipelineError/ValueError",
            ),
        ]
    if log.attempts:
        return [
            CheckResult(iter_id, True),
            CheckResult(creds_id, False, message=f"network on first next(pull({{}})): {log.attempts[0]}"),
        ]
    return [CheckResult(iter_id, True), CheckResult(creds_id, True)]


_GARBAGE_ENVELOPES = (
    ("text", make_text_envelope),
    ("binary", make_binary_envelope),
    ("empty-json", lambda: make_json_envelope({})),
)


def check_push_contract(cls: type) -> list[CheckResult]:
    """``push(garbage, {})`` must return a PushResult without raising or
    touching the network — for every garbage envelope the sink accepts.
    Unaccepted content types may alternatively raise a clean error."""
    check_id = "contract.sink.push-no-creds"
    inst = cls()

    for label, make in _GARBAGE_ENVELOPES:
        envelope: DataEnvelope = make()
        try:
            accepted = bool(inst.accepts(envelope))
        except Exception as e:  # noqa: BLE001
            return [CheckResult(check_id, False, message=f"accepts({label}) raised {type(e).__name__}: {e}")]

        try:
            with no_network() as log:
                result = inst.push(envelope, {})
        except CLEAN_ERRORS as e:
            if accepted:
                return [
                    CheckResult(
                        check_id,
                        False,
                        message=f"push({label}, {{}}) raised {type(e).__name__} for an accepted type — must return PushResult(success=False)",
                    )
                ]
            continue  # clean rejection of an unaccepted type is fine
        except Exception as e:  # noqa: BLE001
            return [CheckResult(check_id, False, message=f"push({label}, {{}}) raised {type(e).__name__}: {e}")]

        if log.attempts:
            return [CheckResult(check_id, False, message=f"network in push({label}, {{}}): {log.attempts[0]}")]
        if not isinstance(result, PushResult):
            return [
                CheckResult(
                    check_id,
                    False,
                    message=f"push({label}, {{}}) returned {type(result).__name__}, expected PushResult",
                )
            ]
    return [CheckResult(check_id, True)]


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


def run_contract_checks(cls: type, entry_point_name: str | None = None) -> list[CheckResult]:
    """All Layer A checks for one connector class.

    Instantiation failure short-circuits the rest — every other check needs
    a working instance. Connector logging is muted for the duration: the
    behavioral checks intentionally poke error paths, and the connectors'
    own error logs would otherwise spray the report.
    """
    import logging

    logging.disable(logging.ERROR)
    try:
        return _run_contract_checks(cls, entry_point_name)
    finally:
        logging.disable(logging.NOTSET)


def _run_contract_checks(cls: type, entry_point_name: str | None) -> list[CheckResult]:
    results = check_instantiation(cls)
    if failures(results):
        return results

    results += check_name(cls)
    results += check_version(cls)
    results += check_auth_type(cls)
    results += check_metadata_spec(cls)
    if entry_point_name is not None:
        results += check_entry_point_name(cls, entry_point_name)

    if issubclass(cls, SourceConnector):
        results += check_output_types(cls)
        results += check_pull_contract(cls)
        results += check_source_validate_config(cls)
    if issubclass(cls, SinkConnector):
        results += check_input_types(cls)
        results += check_push_contract(cls)
        results += check_sink_validate_config(cls)
    return results


# --------------------------------------------------------------------------
# Toolkit checks
# --------------------------------------------------------------------------

#: Longest tool description accepted (LLM tool APIs truncate or reject past this).
MAX_TOOL_DESCRIPTION = 1024

_TOOLKIT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SLUG_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: A slug no toolkit defines — used to probe unknown-tool handling.
UNKNOWN_TOOL_SLUG = "PIPE_COMPLIANCE_NO_SUCH_TOOL"


def _toolkit_catalog(cls: type) -> tuple[list[Any] | None, str | None]:
    """``tools()`` under the socket guard: (catalog, problem)."""
    inst = cls()
    try:
        with no_network() as log:
            catalog = inst.tools()
    except Exception as e:  # noqa: BLE001
        return None, f"tools() raised {type(e).__name__}: {e}"
    if log.attempts:
        return None, f"network in tools(): {log.attempts[0]}"
    if not isinstance(catalog, list):
        return None, f"tools() returned {type(catalog).__name__}, expected list[ToolSpec]"
    return catalog, None


def check_toolkit_name(cls: type) -> list[CheckResult]:
    inst = cls()
    name = getattr(inst, "name", None)
    ok = isinstance(name, str) and bool(_TOOLKIT_NAME_RE.match(name))
    return [
        CheckResult(
            "contract.toolkit.name",
            ok,
            message="" if ok else f"toolkit name {name!r} must be lowercase [a-z0-9_-] starting with a letter",
        )
    ]


def check_toolkit_entry_point_name(cls: type, ep_name: str) -> list[CheckResult]:
    """A toolkit's entry-point name must equal its ``name`` exactly."""
    inst = cls()
    ok = ep_name == getattr(inst, "name", None)
    return [
        CheckResult(
            "contract.toolkit.entry-point-name",
            ok,
            message="" if ok else f"entry point {ep_name!r} does not match toolkit name {inst.name!r}",
        )
    ]


def check_toolkit_catalog(cls: type) -> list[CheckResult]:
    """``tools()`` is a non-empty ``list[ToolSpec]`` with unique UPPER_SNAKE
    slugs under ``slug_prefix``, each resolvable via ``get_tool``."""
    tools_id = "contract.toolkit.tools"
    slugs_id = "contract.toolkit.slugs"
    catalog, problem = _toolkit_catalog(cls)
    if catalog is None:
        return [
            CheckResult(tools_id, False, message=problem or ""),
            CheckResult(slugs_id, False, message="not evaluated — tools() failed"),
        ]
    bad_types = [type(t).__name__ for t in catalog if not isinstance(t, ToolSpec)]
    if not catalog or bad_types:
        message = "tools() returned an empty list" if not catalog else (
            f"tools() items must be ToolSpec, got {', '.join(sorted(set(bad_types)))}"
        )
        return [
            CheckResult(tools_id, False, message=message),
            CheckResult(slugs_id, False, message="not evaluated — tools() is not a list of ToolSpec"),
        ]

    inst = cls()
    prefix = getattr(inst, "slug_prefix", None)
    problems: list[str] = []
    if not isinstance(prefix, str) or not prefix.endswith("_") or not _SLUG_RE.match(prefix):
        problems.append(f"slug_prefix {prefix!r} must be UPPER_SNAKE ending in '_'")
        prefix = None
    seen: set[str] = set()
    for tool in catalog:
        slug = tool.slug
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            problems.append(f"slug {slug!r} is not UPPER_SNAKE")
            continue
        if prefix and (not slug.startswith(prefix) or slug == prefix):
            problems.append(f"slug {slug!r} does not start with slug_prefix {prefix!r}")
        if slug in seen:
            problems.append(f"duplicate slug {slug!r}")
        seen.add(slug)
        if inst.get_tool(slug) != tool:
            problems.append(f"get_tool({slug!r}) does not return its ToolSpec")
    return [
        CheckResult(tools_id, True),
        CheckResult(slugs_id, not problems, message="; ".join(problems)),
    ]


def check_toolkit_schemas(cls: type) -> list[CheckResult]:
    """Every ``input_schema`` is ``{"type": "object", "properties": {...}}``
    with ``required`` a subset of ``properties``."""
    check_id = "contract.toolkit.input-schema"
    catalog, problem = _toolkit_catalog(cls)
    if catalog is None:
        return [CheckResult(check_id, False, message=f"not evaluated — {problem}")]
    problems: list[str] = []
    for tool in catalog:
        if not isinstance(tool, ToolSpec):
            continue
        schema = tool.input_schema
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            problems.append(f"{tool.slug}: input_schema must have type 'object'")
            continue
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            problems.append(f"{tool.slug}: input_schema.properties must be an object")
            continue
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            problems.append(f"{tool.slug}: input_schema.required must be a list of strings")
            continue
        extra = [r for r in required if r not in properties]
        if extra:
            problems.append(f"{tool.slug}: required {extra} not in properties")
    return [CheckResult(check_id, not problems, message="; ".join(problems))]


def check_toolkit_descriptions(cls: type) -> list[CheckResult]:
    check_id = "contract.toolkit.descriptions"
    catalog, problem = _toolkit_catalog(cls)
    if catalog is None:
        return [CheckResult(check_id, False, message=f"not evaluated — {problem}")]
    problems: list[str] = []
    for tool in catalog:
        if not isinstance(tool, ToolSpec):
            continue
        description = tool.description
        if not isinstance(description, str) or not description.strip():
            problems.append(f"{tool.slug}: description is empty")
        elif len(description) > MAX_TOOL_DESCRIPTION:
            problems.append(f"{tool.slug}: description is {len(description)} chars (max {MAX_TOOL_DESCRIPTION})")
    return [CheckResult(check_id, not problems, message="; ".join(problems))]


def _call_tool_check(cls: type, slug: str, label: str) -> str | None:
    """Problem with ``call_tool(slug, {}, {})``, or None when it returned a
    failed ToolResult without raising or touching the network."""
    inst = cls()
    try:
        with no_network() as log:
            result = inst.call_tool(slug, {}, {})
    except Exception as e:  # noqa: BLE001
        return f"call_tool({label}, {{}}, {{}}) raised {type(e).__name__}: {e}"
    if log.attempts:
        return f"network in call_tool({label}, {{}}, {{}}): {log.attempts[0]}"
    if not isinstance(result, ToolResult):
        return f"call_tool({label}, {{}}, {{}}) returned {type(result).__name__}, expected ToolResult"
    if result.successful is not False:
        return f"call_tool({label}, {{}}, {{}}) succeeded — empty config cannot hold credentials"
    if not result.error:
        return f"call_tool({label}, {{}}, {{}}) failed without an error message"
    if result.error_code not in ERROR_CODES:
        return f"call_tool({label}, {{}}, {{}}) returned non-canonical error_code {result.error_code!r}"
    return None


def check_toolkit_call_unknown(cls: type) -> list[CheckResult]:
    problem = _call_tool_check(cls, UNKNOWN_TOOL_SLUG, "<unknown slug>")
    return [CheckResult("contract.toolkit.call-unknown", problem is None, message=problem or "")]


def check_toolkit_call_no_creds(cls: type) -> list[CheckResult]:
    """``call_tool(slug, {}, {})`` fails cleanly for every real slug.

    An ``AuthType.NONE`` toolkit legitimately runs tools without credentials,
    so only its tools with required arguments are probed (they must fail
    argument validation before any I/O)."""
    check_id = "contract.toolkit.call-no-creds"
    catalog, problem = _toolkit_catalog(cls)
    if catalog is None:
        return [CheckResult(check_id, False, message=f"not evaluated — {problem}")]
    probe = [tool for tool in catalog if isinstance(tool, ToolSpec)]
    if cls().auth_type is AuthType.NONE:
        probe = [
            tool
            for tool in probe
            if isinstance(tool.input_schema, Mapping) and tool.input_schema.get("required")
        ]
    problems = [
        p for tool in probe for p in [_call_tool_check(cls, tool.slug, tool.slug)] if p is not None
    ]
    return [CheckResult(check_id, not problems, message="; ".join(problems))]


def check_toolkit_validate_config(cls: type) -> list[CheckResult]:
    return _validate_config_check(cls, "contract.toolkit.validate-config-no-creds")


def check_toolkit_search(cls: type) -> list[CheckResult]:
    check_id = "contract.toolkit.search"
    inst = cls()
    try:
        with no_network() as log:
            found = inst.search_tools("list search get", limit=3)
    except Exception as e:  # noqa: BLE001
        return [CheckResult(check_id, False, message=f"search_tools() raised {type(e).__name__}: {e}")]
    if log.attempts:
        return [CheckResult(check_id, False, message=f"network in search_tools(): {log.attempts[0]}")]
    ok = isinstance(found, list) and len(found) <= 3 and all(isinstance(t, ToolSpec) for t in found)
    return [
        CheckResult(
            check_id,
            ok,
            message="" if ok else "search_tools(query, limit=3) must return at most 3 ToolSpecs",
        )
    ]


def check_toolkit_available_tools(cls: type) -> list[CheckResult]:
    """``available_tools(None)`` and ``available_tools({})`` run offline,
    don't raise, and return a list of ToolSpec drawn from the catalog (each
    one resolvable through ``get_tool``)."""
    check_id = "contract.toolkit.available-tools"
    inst = cls()
    problems: list[str] = []
    for label, config in (("None", None), ("{}", {})):
        error: Exception | None = None
        listed: Any = None
        with no_network() as log:
            try:
                listed = inst.available_tools(config)
            except Exception as e:  # noqa: BLE001 — reported below
                error = e
        if log.attempts:  # before the error: the guard raises on a blocked dial
            problems.append(f"network in available_tools({label}): {log.attempts[0]}")
            continue
        if error is not None:
            problems.append(f"available_tools({label}) raised {type(error).__name__}: {error}")
            continue
        if not isinstance(listed, list) or not all(isinstance(t, ToolSpec) for t in listed):
            problems.append(f"available_tools({label}) must return a list of ToolSpec")
            continue
        stray = [t.slug for t in listed if inst.get_tool(t.slug) != t]
        if stray:
            problems.append(
                f"available_tools({label}) lists tools outside the catalog: {', '.join(map(str, stray[:5]))}"
            )
    return [CheckResult(check_id, not problems, message="; ".join(problems))]


#: An OAuth scope token (RFC 6749 §3.3: printable ASCII but space, ``"`` and
#: ``\``), minus the comma some providers separate scopes with.
_SCOPE_RE = re.compile(r"^[\x21\x23-\x2b\x2d-\x5b\x5d-\x7e]{1,256}$")
#: Longest keyword and example a card may carry.
MAX_KEYWORD = 40
MAX_EXAMPLE = 120


def check_toolkit_card(cls: type) -> list[CheckResult]:
    """``card()`` runs offline and returns a JSON-serializable dict with
    exactly :data:`CARD_FIELDS`, typed as documented, naming the toolkit."""
    check_id = "contract.toolkit.card"
    try:
        with no_network() as log:
            card = cls.card()  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        return [CheckResult(check_id, False, message=f"card() raised {type(e).__name__}: {e}")]
    if log.attempts:
        return [CheckResult(check_id, False, message=f"network in card(): {log.attempts[0]}")]
    if not isinstance(card, dict):
        return [CheckResult(check_id, False, message=f"card() returned {type(card).__name__}, expected dict")]
    problems: list[str] = []
    if set(card) != set(CARD_FIELDS):
        problems.append(f"card() keys {sorted(card)} != {sorted(CARD_FIELDS)}")
    for key in ("name", "display_name", "description", "category", "auth_type"):
        if key in card and not isinstance(card[key], str):
            problems.append(f"card()[{key!r}] must be a string")
    for key in ("keywords", "examples"):
        if key in card and not (
            isinstance(card[key], list) and all(isinstance(v, str) for v in card[key])
        ):
            problems.append(f"card()[{key!r}] must be a list of strings")
    if card.get("name") != getattr(cls, "name", None):
        problems.append(f"card()['name'] {card.get('name')!r} != toolkit name {getattr(cls, 'name', None)!r}")
    try:
        json.dumps(card)
    except (TypeError, ValueError) as e:
        problems.append(f"card() is not JSON-serializable: {e}")
    return [CheckResult(check_id, not problems, message="; ".join(problems))]


def check_toolkit_category(cls: type) -> list[CheckResult]:
    category = getattr(cls, "category", None)
    ok = category in CATEGORIES
    return [
        CheckResult(
            "contract.toolkit.category",
            ok,
            message="" if ok else f"category {category!r} is not one of: {', '.join(sorted(CATEGORIES))}",
        )
    ]


def check_toolkit_card_text(cls: type) -> list[CheckResult]:
    """``keywords`` is a tuple of short words; ``examples`` is empty or 2-4
    short user asks. Neither may be a bare string."""
    problems: list[str] = []
    keywords = getattr(cls, "keywords", ())
    if not isinstance(keywords, (tuple, list)):
        problems.append(f"keywords must be a tuple of strings, got {type(keywords).__name__}")
    else:
        bad = [k for k in keywords if not (isinstance(k, str) and k.strip() and len(k) <= MAX_KEYWORD)]
        if bad:
            problems.append(f"keywords must be non-empty strings of at most {MAX_KEYWORD} chars: {bad[:3]!r}")
    examples = getattr(cls, "examples", ())
    if not isinstance(examples, (tuple, list)):
        problems.append(f"examples must be a tuple of strings, got {type(examples).__name__}")
    elif examples:
        if not 2 <= len(examples) <= 4:
            problems.append(f"examples must hold 2-4 user asks, got {len(examples)}")
        bad = [e for e in examples if not (isinstance(e, str) and e.strip() and len(e) <= MAX_EXAMPLE)]
        if bad:
            problems.append(f"examples must be non-empty strings of at most {MAX_EXAMPLE} chars: {bad[:2]!r}")
    return [CheckResult("contract.toolkit.card-text", not problems, message="; ".join(problems))]


def check_toolkit_tool_metadata(cls: type) -> list[CheckResult]:
    """Every tool's ``required_scopes`` is a tuple of distinct, real-looking
    OAuth scope strings, and its ``risk`` is a known value no lower than its
    flags say (a ``read_only`` tool is ``read``, a ``destructive`` one
    ``destructive``)."""
    check_id = "contract.toolkit.tool-metadata"
    catalog, problem = _toolkit_catalog(cls)
    if catalog is None:
        return [CheckResult(check_id, False, message=f"not evaluated — {problem}")]
    problems: list[str] = []
    for tool in catalog:
        if not isinstance(tool, ToolSpec):
            continue
        scopes = tool.required_scopes
        if not isinstance(scopes, tuple):
            problems.append(f"{tool.slug}: required_scopes must be a tuple")
        else:
            bad = [s for s in scopes if not (isinstance(s, str) and _SCOPE_RE.match(s))]
            if bad:
                problems.append(f"{tool.slug}: required_scopes {bad[:3]!r} do not look like OAuth scopes")
            if len(set(scopes)) != len(scopes):
                problems.append(f"{tool.slug}: required_scopes has duplicates")
        if tool.risk not in RISKS:
            problems.append(f"{tool.slug}: risk {tool.risk!r} is not one of: {', '.join(RISKS)}")
            continue
        floor = "destructive" if tool.destructive else "read" if tool.read_only else "write"
        if RISKS.index(tool.risk) < RISKS.index(floor) or (tool.read_only and tool.risk != "read"):
            problems.append(
                f"{tool.slug}: risk {tool.risk!r} contradicts read_only={tool.read_only}, "
                f"destructive={tool.destructive}"
            )
    return [CheckResult(check_id, not problems, message="; ".join(problems))]


def check_toolkit_scopes_requested(cls: type) -> list[CheckResult]:
    """Warning: a tool needs a scope the toolkit's default consent
    (``oauth.default_scopes``) does not ask for, so a connection made with
    the defaults never lists it. Skipped without default scopes."""
    check_id = "contract.toolkit.scopes-requested"
    inst = cls()
    spec = getattr(inst, "oauth", None)
    defaults = frozenset(spec.default_scopes) if isinstance(spec, OAuthSpec) else frozenset()
    catalog, _ = _toolkit_catalog(cls)
    if not defaults or not catalog:
        return [CheckResult(check_id, True, severity=Severity.WARNING)]
    missing = sorted(
        {
            s
            for tool in catalog
            if isinstance(tool, ToolSpec)
            for s in tool.required_scopes
            if isinstance(s, str) and not inst.scope_granted(s, defaults)
        }
    )
    return [
        CheckResult(
            check_id,
            not missing,
            severity=Severity.WARNING,
            message="" if not missing else (
                f"tools need scopes oauth.default_scopes does not request: {', '.join(missing[:5])}"
            ),
        )
    ]


def check_toolkit_slug_prefix_unique(cls: type, installed: Mapping[str, type]) -> list[CheckResult]:
    """The toolkit's ``slug_prefix`` collides with no other installed
    toolkit's (``installed``: name -> class): not equal, and neither starts
    the other, so a bare slug names one toolkit."""
    name = getattr(cls, "name", None)
    others = {n: c for n, c in installed.items() if n != name and c is not cls}
    clashes = sorted(
        other
        for pair in slug_prefix_conflicts({**others, str(name): cls})
        if str(name) in pair
        for other in pair
        if other != str(name)
    )
    ok = not clashes
    return [
        CheckResult(
            "contract.toolkit.slug-prefix-unique",
            ok,
            message="" if ok else (
                f"slug_prefix {getattr(cls, 'slug_prefix', None)!r} collides with "
                + ", ".join(f"{o} ({getattr(others[o], 'slug_prefix', None)!r})" for o in clashes)
            ),
        )
    ]


def _is_https(url: Any) -> bool:
    return isinstance(url, str) and url.startswith("https://") and len(url) > len("https://")


def check_toolkit_oauth(cls: type) -> list[CheckResult]:
    """An OAuth spec, when declared, is an OAuthSpec with https endpoints.
    An OAUTH2 toolkit without one is flagged as a warning."""
    inst = cls()
    spec = getattr(inst, "oauth", None)
    if spec is None:
        ok = inst.auth_type is not AuthType.OAUTH2
        return [
            CheckResult(
                "contract.toolkit.oauth-declared",
                ok,
                severity=Severity.WARNING,
                message="" if ok else "auth_type is OAUTH2 but no OAuthSpec is declared (oauth = None)",
            )
        ]
    problems: list[str] = []
    if not isinstance(spec, OAuthSpec):
        problems.append(f"oauth must be an OAuthSpec, got {type(spec).__name__}")
    else:
        if not spec.provider:
            problems.append("oauth.provider is empty")
        for attr in ("authorize_url", "token_url"):
            if not _is_https(getattr(spec, attr)):
                problems.append(f"oauth.{attr} must be an https URL")
        if spec.revoke_url is not None and not _is_https(spec.revoke_url):
            problems.append("oauth.revoke_url must be an https URL")
        reserved = sorted(set(spec.extra_authorize_params) & FLOW_AUTHORIZE_PARAMS)
        if reserved:
            problems.append(
                f"oauth.extra_authorize_params may not set flow parameters: {', '.join(reserved)}"
            )
    return [CheckResult("contract.toolkit.oauth", not problems, message="; ".join(problems))]


def run_toolkit_contract_checks(
    cls: type,
    entry_point_name: str | None = None,
    *,
    installed: Mapping[str, type] | None = None,
) -> list[CheckResult]:
    """All Layer A checks for one toolkit class (logging muted, as for
    connectors). With ``installed`` (name -> class of every installed
    toolkit) the slug prefix is also checked against the others."""
    import logging

    logging.disable(logging.ERROR)
    try:
        return _run_toolkit_contract_checks(cls, entry_point_name, installed)
    finally:
        logging.disable(logging.NOTSET)


def _run_toolkit_contract_checks(
    cls: type, entry_point_name: str | None, installed: Mapping[str, type] | None
) -> list[CheckResult]:
    if not (isinstance(cls, type) and issubclass(cls, Toolkit)):
        return [
            CheckResult(
                "contract.toolkit.base-class",
                False,
                message=f"{getattr(cls, '__name__', cls)!r} is not a Toolkit subclass",
            )
        ]
    results = check_instantiation(cls)
    if failures(results):
        return results
    results += check_toolkit_name(cls)
    results += check_version(cls)
    results += check_auth_type(cls)
    if entry_point_name is not None:
        results += check_toolkit_entry_point_name(cls, entry_point_name)
    results += check_toolkit_catalog(cls)
    results += check_toolkit_schemas(cls)
    results += check_toolkit_descriptions(cls)
    results += check_toolkit_search(cls)
    results += check_toolkit_available_tools(cls)
    results += check_toolkit_call_unknown(cls)
    results += check_toolkit_call_no_creds(cls)
    results += check_toolkit_validate_config(cls)
    results += check_toolkit_oauth(cls)
    results += check_toolkit_card(cls)
    results += check_toolkit_category(cls)
    results += check_toolkit_card_text(cls)
    results += check_toolkit_tool_metadata(cls)
    results += check_toolkit_scopes_requested(cls)
    if installed is not None:
        results += check_toolkit_slug_prefix_unique(cls, installed)
    return results
