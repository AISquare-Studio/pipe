"""Composio SDK wrapper used by all Composio connectors.

Centralises auth, retry-on-rate-limit, error mapping, and pydantic→dict
normalisation so the connector classes stay thin and SDK-version drift is
contained to this one module. Nothing outside this file (and its tests)
imports the ``composio`` / ``composio_client`` packages.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from composio import Composio
from composio.exceptions import ComposioError
from composio_client import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_composio.constants import (
    CURSOR_FILENAME,
    DEFAULT_RESOURCE_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TOOL_LIMIT,
    DEFAULT_TRIGGER_PAGE_LIMIT,
    DEFAULT_USER_ID,
    DOWNLOADS_SUBDIR,
    FILES_SUBDIR,
    INITIAL_BACKOFF,
    MAX_RETRIES,
    SEEN_IDS_MAX,
    TRIGGER_LOG_SDK_RETRIES,
    UPLOADS_SUBDIR,
)

logger = logging.getLogger("aisquare.pipe.composio")

# Where each supported composio-client keeps the trigger-log resource, newest
# first: 2.x moved ``client.logs.triggers`` to ``client._internal.logs.triggers``
# (x-internal, "no compatibility promise"; its MIGRATION.md, 2.0.0-rc.22).
_TRIGGER_LOG_PATHS: tuple[tuple[str, ...], ...] = (
    ("_internal", "logs", "triggers"),
    ("logs", "triggers"),
)


class ComposioSDKShapeError(PipelineError):
    """The installed composio SDK lacks something this connector calls, or
    calls it differently: an SDK version this connector doesn't support. Not
    transient (retrying can't fix it), so the triggers source raises it
    instead of polling on.

    Only SDK access raises it (``_sdk_call``, ``ComposioClient._trigger_logs``):
    a method that is gone, a call that doesn't bind to the installed
    signature, or a return type the SDK no longer uses. Errors the call itself
    raises, and malformed API data, keep their own types."""


def _sdk_call(
    method: Callable[..., Any] | None, label: str, *args: Any, **kwargs: Any
) -> Any:
    """Call the SDK's ``method`` (``label`` names it in errors) after binding
    the arguments to its signature. A method that is missing, or a call that
    doesn't bind, is ``ComposioSDKShapeError``. A TypeError raised inside the
    call is the SDK's own and propagates as is."""
    if not callable(method):
        raise ComposioSDKShapeError(
            f"the installed composio SDK has no {label}(); "
            "this composio version is not supported"
        )
    return _bound_sdk_call(method, _signature(method), label, *args, **kwargs)


def _bound_sdk_call(
    method: Callable[..., Any],
    signature: inspect.Signature | None,
    label: str,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """``_sdk_call`` for a caller that already holds ``method``'s
    ``_signature`` (None: not introspectable), so it isn't computed twice."""
    if signature is not None:
        try:
            signature.bind(*args, **kwargs)
        except TypeError as e:
            raise ComposioSDKShapeError(
                f"composio SDK {label}() does not take these arguments: {e}"
            ) from e
    return method(*args, **kwargs)


def _signature(method: Callable[..., Any]) -> inspect.Signature | None:
    """``method``'s signature, or None when it can't be introspected (then
    the call itself decides what binds)."""
    try:
        return inspect.signature(method)
    except (TypeError, ValueError):
        return None


def _map_composio_errors(func):  # type: ignore[no-untyped-def]
    """Translate Composio SDK exceptions to framework errors. SDK drift is
    already a ``ComposioSDKShapeError`` (raised by ``_sdk_call``); nothing
    else is turned into one."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (AuthenticationError, PermissionDeniedError) as e:
            raise ConfigValidationError(f"Composio auth failed: {e}") from e
        except RateLimitError as e:
            raise PipelineError(
                "Composio rate limit exceeded after max retries"
            ) from e
        except (APIStatusError, APIConnectionError) as e:
            raise PipelineError(f"Composio API error: {e}") from e
        except ComposioError as e:
            raise PipelineError(f"Composio SDK error: {e}") from e

    return wrapper


def _retry_on_rate_limit(func):  # type: ignore[no-untyped-def]
    """Retry with exponential backoff on HTTP 429."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except RateLimitError:
                if attempt == MAX_RETRIES - 1:
                    raise
                wait = INITIAL_BACKOFF * (2**attempt)
                logger.warning(
                    "Composio rate-limited (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
        raise PipelineError("Composio rate limit exceeded after max retries")

    return wrapper


def _to_plain(obj: Any) -> Any:
    """Recursively convert pydantic models to plain dicts/lists/scalars."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


def _items(response: Any) -> list[dict[str, Any]]:
    """Extract the item list from a Composio list response, tolerating both
    ``items`` and ``data`` field spellings across resource types."""
    for attr in ("items", "data"):
        value = getattr(response, attr, None)
        if value is None and isinstance(response, dict):
            value = response.get(attr)
        if isinstance(value, list):
            return [_to_plain(v) for v in value]
    return []


def _epoch_ms(timestamp: str | int | float | None) -> int | None:
    """Coerce an ISO-8601 string or epoch value to epoch milliseconds."""
    if timestamp is None:
        return None
    if isinstance(timestamp, (int, float)):
        # Heuristic: values below 1e12 are seconds, not milliseconds.
        try:
            return int(timestamp if timestamp >= 1e12 else timestamp * 1000)
        except (OverflowError, ValueError):  # inf or nan: not a time
            return None
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def default_state_dir() -> Path:
    """Per-user state root: ``$XDG_CACHE_HOME/aisquare-pipe`` when set, else
    ``~/.cache/aisquare-pipe``. Deliberately not a shared tempdir — fixed
    names under world-writable ``/tmp`` collide across users and expose
    cursor/file state."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "aisquare-pipe"


def default_file_workdir() -> Path:
    """Directory used for file uploads/downloads when none is configured."""
    return default_state_dir() / FILES_SUBDIR


def default_cursor_path() -> str:
    """Per-user default location of the triggers cursor file."""
    return str(default_state_dir() / CURSOR_FILENAME)


def migrate_legacy_cursor(legacy_path: str, new_path: str) -> None:
    """One-time copy of a pre-0.1.1 shared-``/tmp`` cursor file into the
    per-user state dir. Runs only while the new file does not exist; the
    legacy file is left in place for instances still on older versions."""
    legacy, new = Path(legacy_path), Path(new_path)
    try:
        if new.exists() or not legacy.exists():
            return
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(legacy.read_bytes())
        logger.info("Migrated trigger cursor from %s to %s", legacy, new)
    except OSError as e:
        logger.warning("Could not migrate legacy cursor %s: %s", legacy_path, e)


class ComposioClient:
    """Thin wrapper around ``composio.Composio``.

    All methods return plain dicts/lists. Construct with ``file_mode=True``
    to enable the SDK's automatic file upload/download handling: tool
    arguments holding paths inside the upload workdir are uploaded, and
    file outputs are downloaded under the download workdir and replaced
    with local path strings in the result.
    """

    def __init__(self, config: dict[str, Any], *, file_mode: bool = False) -> None:
        api_key = config.get("api_key")
        if not api_key or not isinstance(api_key, str):
            raise ConfigValidationError(
                "composio config requires 'api_key' (Composio API key)"
            )

        workdir = Path(config.get("file_workdir") or default_file_workdir())
        self.download_dir: Path | None = None
        self.upload_dir: Path | None = None

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            "allow_tracking": bool(config.get("allow_tracking", False)),
        }
        if config.get("base_url"):
            kwargs["base_url"] = config["base_url"]
        if file_mode:
            self.download_dir = workdir / DOWNLOADS_SUBDIR
            self.upload_dir = workdir / UPLOADS_SUBDIR
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            kwargs["dangerously_allow_auto_upload_download_files"] = True
            kwargs["file_download_dir"] = str(self.download_dir)
            # Only files materialised by this connector may be uploaded.
            kwargs["file_upload_dirs"] = [str(self.upload_dir)]

        self._sdk = Composio(**kwargs)

    def _call(self, path: str, *args: Any, **kwargs: Any) -> Any:
        """Call the SDK method at dotted ``path`` under the ``Composio``
        object, through ``_sdk_call``: a missing method or a call that doesn't
        bind is ``ComposioSDKShapeError``."""
        node: Any = self._sdk
        for part in path.split("."):
            node = getattr(node, part, None)
        return _sdk_call(node, path, *args, **kwargs)

    # -- core -----------------------------------------------------------

    @_map_composio_errors
    def validate(self) -> bool:
        """Make a cheap call to confirm the API key works."""
        self._call("toolkits.list", limit=1)
        return True

    @_map_composio_errors
    @_retry_on_rate_limit
    def execute_tool(
        self,
        slug: str,
        *,
        user_id: str = DEFAULT_USER_ID,
        arguments: dict[str, Any] | None = None,
        connected_account_id: str | None = None,
        tool_version: str | None = None,
    ) -> dict[str, Any]:
        """Execute a tool and return its result ``data`` dict.

        Raises PipelineError when Composio reports the execution as
        unsuccessful, with the tool's error text included.
        """
        kwargs: dict[str, Any] = {"user_id": user_id}
        if connected_account_id:
            kwargs["connected_account_id"] = connected_account_id
        if tool_version:
            kwargs["version"] = tool_version
        else:
            # No explicit version: target the latest toolkit version. Composio's
            # manual execution rejects "latest" and otherwise raises
            # ToolVersionRequiredError, so skip the version check (resolves to
            # latest) when the caller hasn't pinned one.
            kwargs["dangerously_skip_version_check"] = True
        response = _to_plain(
            self._call("tools.execute", slug, arguments or {}, **kwargs)
        )
        if not isinstance(response, dict):
            raise ComposioSDKShapeError(
                f"composio SDK tools.execute() returned a {type(response).__name__}, "
                "not a dict"
            )
        successful = bool(response.get("successful"))
        if not successful:
            error = response.get("error") or "unknown error"
            raise PipelineError(f"Composio tool '{slug}' failed: {error}")
        return _to_plain(response.get("data") or {})

    # -- catalog --------------------------------------------------------

    @_map_composio_errors
    @_retry_on_rate_limit
    def list_toolkits(self, *, limit: int = DEFAULT_RESOURCE_LIMIT) -> list[dict[str, Any]]:
        return _items(self._call("toolkits.list", limit=limit))

    @_map_composio_errors
    @_retry_on_rate_limit
    def list_tools(
        self,
        *,
        toolkit: str | None = None,
        search: str | None = None,
        limit: int = DEFAULT_TOOL_LIMIT,
    ) -> list[dict[str, Any]]:
        tools = self._call(
            "tools.get_raw_composio_tools",
            toolkits=[toolkit] if toolkit else None,
            search=search,
            limit=limit,
        )
        return [_to_plain(t) for t in tools]

    @_map_composio_errors
    def get_tool(self, slug: str) -> dict[str, Any]:
        return _to_plain(self._call("tools.get_raw_composio_tool_by_slug", slug))

    # -- connected accounts ----------------------------------------------

    @_map_composio_errors
    @_retry_on_rate_limit
    def list_connected_accounts(
        self,
        *,
        user_id: str | None = None,
        toolkits: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if user_id:
            kwargs["user_ids"] = [user_id]
        if toolkits:
            kwargs["toolkit_slugs"] = toolkits
        return _items(self._call("connected_accounts.list", **kwargs))

    @_map_composio_errors
    def authorize_toolkit(self, *, user_id: str, toolkit: str) -> dict[str, Any]:
        """Start an OAuth connection using the toolkit's default auth config."""
        request = self._call("toolkits.authorize", user_id=user_id, toolkit=toolkit)
        return self._connection_request_to_dict(request)

    @_map_composio_errors
    def initiate_connection(
        self,
        *,
        user_id: str,
        auth_config_id: str,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        request = self._call(
            "connected_accounts.initiate",
            user_id,
            auth_config_id,
            callback_url=callback_url,
        )
        return self._connection_request_to_dict(request)

    @_map_composio_errors
    def wait_for_connection(
        self, connection_id: str, *, timeout: float = 300.0
    ) -> dict[str, Any]:
        return _to_plain(
            self._call(
                "connected_accounts.wait_for_connection", connection_id, timeout=timeout
            )
        )

    @_map_composio_errors
    @_retry_on_rate_limit
    def list_auth_configs(self, *, toolkit: str | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if toolkit:
            kwargs["toolkit_slug"] = toolkit
        return _items(self._call("auth_configs.list", **kwargs))

    @staticmethod
    def _connection_request_to_dict(request: Any) -> dict[str, Any]:
        return {
            "id": getattr(request, "id", None),
            "status": getattr(request, "status", None),
            "redirect_url": getattr(request, "redirect_url", None),
        }

    # -- triggers ---------------------------------------------------------

    @_map_composio_errors
    @_retry_on_rate_limit
    def list_trigger_types(
        self, *, toolkits: list[str] | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if toolkits:
            kwargs["toolkit_slugs"] = toolkits
        return _items(self._call("triggers.list", **kwargs))

    @_map_composio_errors
    @_retry_on_rate_limit
    def list_trigger_instances(
        self, *, trigger_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if trigger_ids:
            kwargs["trigger_ids"] = trigger_ids
        return _items(self._call("triggers.list_active", **kwargs))

    @_map_composio_errors
    @_retry_on_rate_limit
    def list_trigger_events(
        self,
        *,
        user_id: str | None = None,
        from_ms: int | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_TRIGGER_PAGE_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return ``(events_oldest_first, next_cursor)`` from the trigger
        event log.

        Uses the trigger-logs endpoint (``/api/v3.1/internal/trigger/logs``)
        — the only poll-style event surface Composio exposes; the SDK's
        first-class alternative is a realtime websocket subscription.

        A malformed log entry never raises: it comes back as an event with
        ``skipped`` set (see ``_normalize_trigger_event``), so the caller can
        step past it.
        """
        kwargs: dict[str, Any] = {
            "limit": limit,
            "include_payload": True,
            "status": "success",
        }
        if user_id:
            kwargs["entity_id"] = user_id
        if from_ms is not None:
            kwargs["from_"] = from_ms
        if cursor:
            kwargs["cursor"] = cursor
        list_logs = self._trigger_logs().list
        signature = _signature(list_logs)
        if signature is not None and "request_options" in signature.parameters:
            # composio-client 2.x gives a POST no SDK retries (1.x retries
            # every method). This POST only reads, so it asks for 1.x's.
            kwargs["request_options"] = {"max_retries": TRIGGER_LOG_SDK_RETRIES}
        # _trigger_logs() checked list is callable; bind it on this signature.
        response = _bound_sdk_call(list_logs, signature, "trigger-log list", **kwargs)
        events = [self._normalize_trigger_event(item) for item in _items(response)]
        events.sort(key=lambda e: e.get("timestamp_ms") or 0)
        next_cursor = getattr(response, "next_cursor", None)
        return events, next_cursor

    def _trigger_logs(self) -> Any:
        """The installed SDK's trigger-log resource (``_TRIGGER_LOG_PATHS``),
        or ``ComposioSDKShapeError`` naming where it looked."""
        for path in _TRIGGER_LOG_PATHS:
            node: Any = self._sdk.client
            for part in path:
                node = getattr(node, part, None)
                if node is None:
                    break
            if node is not None and callable(getattr(node, "list", None)):
                return node
        looked = " or ".join("client." + ".".join(p) for p in _TRIGGER_LOG_PATHS)
        raise ComposioSDKShapeError(
            f"the installed composio SDK has no trigger-log resource ({looked}); "
            "this composio-client version is not supported"
        )

    @staticmethod
    def _normalize_trigger_event(item: Any) -> dict[str, Any]:
        """One trigger-log entry as a flat event. An entry is API data, not
        SDK shape, so a malformed one (the entry or its ``meta`` not an
        object) never raises. It comes back with ``skipped`` (the reason,
        naming types only) and whatever could be read: ``id`` and
        ``timestamp_ms``, so the poller can record it and step past."""
        if not isinstance(item, dict):
            return _skipped_event(None, f"the entry is a {type(item).__name__}")
        meta = item.get("meta") or {}
        if not isinstance(meta, dict):
            return _skipped_event(item, f"its meta is a {type(meta).__name__}")
        payload_raw = (
            meta.get("trigger_provider_payload")
            or meta.get("trigger_client_payload")
        )
        payload: Any = None
        if isinstance(payload_raw, str) and payload_raw:
            try:
                payload = json.loads(payload_raw)
            except ValueError:
                payload = payload_raw
        elif payload_raw is not None:
            payload = payload_raw

        timestamp = item.get("created_at")
        return {
            "id": item.get("id"),
            "trigger_slug": meta.get("trigger_name"),
            "trigger_id": meta.get("trigger_nano_id") or meta.get("trigger_id"),
            "toolkit": item.get("app_name"),
            "connected_account_id": item.get("connection_id"),
            "user_id": item.get("entity_id"),
            "timestamp": timestamp,
            "timestamp_ms": _epoch_ms(timestamp),
            "status": item.get("status"),
            "payload": payload,
        }


def _skipped_event(item: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    """The event for a malformed trigger-log entry: only its id and time."""
    timestamp = item.get("created_at") if item is not None else None
    return {
        "id": item.get("id") if item is not None else None,
        "timestamp": timestamp,
        "timestamp_ms": _epoch_ms(timestamp),
        "skipped": f"malformed trigger-log entry ({reason}, not an object)",
    }


@dataclass
class TriggerCursor:
    """Persisted polling position for the triggers source.

    ``pending_cursor``/``pending_max_ts`` carry an interrupted window across
    polls: when one poll cycle hits its page cap before draining the event
    log, the next cycle resumes from the saved page cursor instead of
    re-reading (or worse, skipping) the remainder. The watermark only
    advances once a window is fully drained.
    """

    last_ts_ms: int = 0
    seen_ids: list[str] = field(default_factory=list)
    pending_cursor: str | None = None
    pending_max_ts: int = 0


def load_trigger_cursor(path: str) -> TriggerCursor:
    """Read the trigger cursor from disk, defaulting to an empty cursor."""
    p = Path(path)
    if not p.exists():
        return TriggerCursor()
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(
            "Failed to read trigger cursor at %s: %s — starting fresh", path, e
        )
        return TriggerCursor()
    try:
        last_ts_ms = int(payload.get("last_ts_ms", 0))
    except (TypeError, ValueError):
        last_ts_ms = 0
    seen = payload.get("seen_ids", [])
    seen_ids = [str(s) for s in seen] if isinstance(seen, list) else []
    pending_cursor = payload.get("pending_cursor")
    if pending_cursor is not None:
        pending_cursor = str(pending_cursor)
    try:
        pending_max_ts = int(payload.get("pending_max_ts", 0))
    except (TypeError, ValueError):
        pending_max_ts = 0
    return TriggerCursor(
        last_ts_ms=last_ts_ms,
        seen_ids=seen_ids,
        pending_cursor=pending_cursor,
        pending_max_ts=pending_max_ts,
    )


def save_trigger_cursor(path: str, state: TriggerCursor) -> None:
    """Atomically persist the trigger cursor, trimming the dedup ring."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "last_ts_ms": int(state.last_ts_ms),
                "seen_ids": state.seen_ids[-SEEN_IDS_MAX:],
                "pending_cursor": state.pending_cursor,
                "pending_max_ts": int(state.pending_max_ts),
            }
        ),
        encoding="utf-8",
    )
    tmp.replace(p)
