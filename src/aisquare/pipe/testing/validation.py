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

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from importlib.metadata import entry_points
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PushResult
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
    """One ``aisquare_pipe.connectors`` entry point, load failures included."""

    ep_name: str
    ep_value: str
    dist_name: str | None
    dist_version: str | None
    cls: type | None
    load_error: str | None


def discover_connector_entry_points() -> list[DiscoveredConnector]:
    """All connector entry points with dist identity and load outcome.

    Unlike :func:`aisquare.pipe.core.registry.discover_connectors`, load
    failures are returned (not just logged) and the owning distribution is
    reported — both are needed to attribute problems to a connector package.
    """
    discovered: list[DiscoveredConnector] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
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
            )
        )
    return sorted(discovered, key=lambda d: d.ep_name)


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
