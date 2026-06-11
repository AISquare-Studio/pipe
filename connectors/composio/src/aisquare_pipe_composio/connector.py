"""Composio source and sink connectors for aisquare.pipe.

One connector pair bridges pipe to Composio's full catalog (~500 SaaS
toolkits): the source executes any Composio tool and yields its results;
the sink executes any Composio tool with envelope data as arguments.
Composio manages the third-party OAuth via per-user connected accounts —
the only credential this connector needs is a Composio API key.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    RateLimit,
    Resource,
)

from aisquare_pipe_composio.client import ComposioClient
from aisquare_pipe_composio.constants import (
    DEFAULT_USER_ID,
    JSON_CONTENT_TYPE,
    RATE_LIMIT_RPS,
)
from aisquare_pipe_composio.files import (
    find_downloaded_files,
    guess_content_type,
    materialize_upload_file,
)

logger = logging.getLogger("aisquare.pipe.composio")

COMPOSIO_DOCS_URL = "https://docs.composio.dev"

# Config keys shared by all Composio connectors (documented in README.md):
#   api_key (str, required)        — Composio API key
#   user_id (str)                  — Composio entity tools run as (default "default")
#   connected_account_id (str)     — pin execution to one connected account
#   toolkit_filter (list[str])     — allow-list of toolkit slugs
#   base_url (str)                 — Composio backend override
#   timeout_seconds (int)          — SDK request timeout (default 60)
#   file_workdir (str)             — directory for file uploads/downloads


def _normalize_slug(tool: str) -> str:
    """Canonicalise a tool slug: ``gmail_fetch_emails`` → ``GMAIL_FETCH_EMAILS``."""
    return str(tool).strip().upper()


def _toolkit_prefix(toolkit: str) -> str:
    """Tool-slug prefix for a toolkit: ``gmail`` → ``GMAIL_``."""
    return toolkit.strip().upper().replace("-", "_") + "_"


def _belongs_to_toolkit(slug: str, toolkit: str) -> bool:
    return slug.startswith(_toolkit_prefix(toolkit))


def _toolkit_of(slug: str) -> str:
    """Best-effort toolkit slug from a tool slug (first ``_`` segment,
    lowercased). Heuristic — used for metadata, not enforcement."""
    return slug.split("_", 1)[0].lower()


def _check_toolkit_allowed(
    slug: str, config: dict, pinned: str | None
) -> None:
    """Raise ValueError when the pinned toolkit or config toolkit_filter
    excludes this tool slug."""
    if pinned and not _belongs_to_toolkit(slug, pinned):
        raise ValueError(
            f"Tool '{slug}' does not belong to toolkit '{pinned}' "
            f"(this connector is pinned to '{pinned}')"
        )
    toolkit_filter = config.get("toolkit_filter")
    if toolkit_filter:
        if not any(_belongs_to_toolkit(slug, t) for t in toolkit_filter):
            raise ValueError(
                f"Tool '{slug}' is not allowed by toolkit_filter {toolkit_filter}"
            )


def _coerce_to_dict(data: Any) -> dict[str, Any] | None:
    """Resolve envelope.data → a dict suitable for tool arguments."""
    if isinstance(data, dict):
        return data
    try:
        if isinstance(data, str):
            parsed = json.loads(data)
        elif isinstance(data, bytes):
            parsed = json.loads(data.decode("utf-8"))
        else:
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _walk_path(data: Any, dotted: str) -> Any:
    """Walk a dot-path (list indices allowed) into a tool result."""
    current = data
    for segment in dotted.split("."):
        if isinstance(current, dict):
            if segment not in current:
                raise ValueError(f"unwrap path '{dotted}': key '{segment}' not found")
            current = current[segment]
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError) as e:
                raise ValueError(
                    f"unwrap path '{dotted}': invalid list index '{segment}'"
                ) from e
        else:
            raise ValueError(
                f"unwrap path '{dotted}': cannot descend into {type(current).__name__}"
            )
    return current


def _unwrap_items(data: Any, unwrap: bool | str) -> list[Any] | None:
    """Resolve the list to fan out over, or None for a single envelope.

    ``unwrap=True`` auto-detects: a list result, or a dict with exactly one
    key whose value is a list (the dominant Composio response shape). Any
    other shape silently falls back to a single envelope. An explicit
    dot-path is a user assertion and raises ValueError when it does not
    resolve to a list.
    """
    if unwrap is False or unwrap is None:
        return None
    if unwrap is True:
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and len(data) == 1:
            (value,) = data.values()
            if isinstance(value, list):
                return value
        return None
    target = _walk_path(data, str(unwrap))
    if not isinstance(target, list):
        raise ValueError(
            f"unwrap path '{unwrap}' resolved to {type(target).__name__}, expected a list"
        )
    return target


def _resolve_user(config: dict, params_value: Any) -> str:
    return str(params_value or config.get("user_id") or DEFAULT_USER_ID)


def _validate_config(config: dict) -> bool:
    """Shared validate_config: key presence check, then a cheap API ping."""
    if not config.get("api_key") or not isinstance(config.get("api_key"), str):
        return False
    try:
        return ComposioClient(config).validate()
    except Exception as e:
        logger.warning("composio validate_config failed: %s", e)
        return False


def _toolkit_resources(client: ComposioClient, config: dict) -> list[Resource]:
    """Toolkits as resources, with the user's connection status joined in."""
    user_id = _resolve_user(config, None)
    accounts: dict[str, dict[str, Any]] = {}
    try:
        for account in client.list_connected_accounts(user_id=user_id):
            toolkit = account.get("toolkit") or {}
            slug = (toolkit.get("slug") if isinstance(toolkit, dict) else toolkit) or ""
            if slug:
                accounts[str(slug).lower()] = account
    except Exception:
        logger.exception("composio: listing connected accounts failed")

    toolkit_filter = {t.lower() for t in (config.get("toolkit_filter") or [])}
    resources: list[Resource] = []
    for toolkit in client.list_toolkits():
        slug = str(toolkit.get("slug") or "").lower()
        if not slug or (toolkit_filter and slug not in toolkit_filter):
            continue
        account = accounts.get(slug)
        meta = toolkit.get("meta") or {}
        resources.append(
            Resource(
                id=slug,
                name=str(toolkit.get("name") or slug),
                resource_type="toolkit",
                metadata={
                    "description": meta.get("description", ""),
                    "tools_count": meta.get("tools_count"),
                    "no_auth": toolkit.get("no_auth", False),
                    "connected": bool(
                        account and str(account.get("status", "")).upper() == "ACTIVE"
                    ),
                    "connection_status": (
                        str(account.get("status")) if account else "NOT_CONNECTED"
                    ),
                    "connected_account_id": account.get("id") if account else None,
                },
            )
        )
    return resources


def _tool_resources(client: ComposioClient, toolkits: list[str]) -> list[Resource]:
    resources: list[Resource] = []
    for toolkit in toolkits:
        for tool in client.list_tools(toolkit=toolkit):
            slug = str(tool.get("slug") or "")
            if not slug:
                continue
            resources.append(
                Resource(
                    id=slug,
                    name=str(tool.get("name") or slug),
                    resource_type="tool",
                    metadata={
                        "toolkit": toolkit,
                        "description": tool.get("description", ""),
                    },
                )
            )
    return resources


class ComposioSource(SourceConnector):
    """Execute any Composio tool and yield its results as envelopes."""

    name = "composio-source"
    version = "0.1.0"
    output_types = [JSON_CONTENT_TYPE, "*/*"]
    auth_type = AuthType.API_KEY
    description = "Execute any Composio tool (500+ SaaS toolkits) and yield its results"
    docs_url = COMPOSIO_DOCS_URL

    # Set by factory subclasses to pin this connector to one toolkit.
    toolkit: str | None = None

    metadata_spec = {
        "composio_tool": MetaField(
            type=str, required=True, description="Executed tool slug"
        ),
        "composio_toolkit": MetaField(
            type=str, required=False, description="Toolkit slug (prefix-derived)"
        ),
        "composio_user_id": MetaField(
            type=str, required=True, description="Composio entity the tool ran as"
        ),
        "composio_connected_account_id": MetaField(
            type=str, required=False, description="Connected account used, when pinned"
        ),
        "item_index": MetaField(
            type=int, required=False, description="Position when unwrapping a list result"
        ),
        "item_count": MetaField(
            type=int, required=False, description="Total items when unwrapping"
        ),
        "filename": MetaField(
            type=str, required=False, description="File name (file envelopes only)"
        ),
        "file_field": MetaField(
            type=str, required=False, description="Result field the file came from"
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Execute one Composio tool and yield envelope(s) for its result.

        Supported PullParams keys:
            tool (str, required): tool slug, e.g. "GMAIL_FETCH_EMAILS"
            arguments (dict): tool input arguments (default {})
            unwrap (bool | str): fan a list result out into one envelope per
                item. True = auto-detect; "dot.path" = explicit path to the
                list (raises if not a list); default False = single envelope.
            download_files (bool): also yield a bytes envelope per file
                output (default False)
            user_id (str): override config user_id for this pull
            connected_account_id (str): override config value for this pull
            tool_version (str): pin a Composio tool version
        """
        if params is None:
            params = PullParams()

        tool = params.get("tool")
        if not tool:
            raise ValueError("composio-source.pull requires params['tool']")
        slug = _normalize_slug(tool)
        _check_toolkit_allowed(slug, config, self.toolkit)

        user_id = _resolve_user(config, params.get("user_id"))
        connected_account_id = params.get(
            "connected_account_id", config.get("connected_account_id")
        )
        download_files = bool(params.get("download_files", False))

        client = ComposioClient(config, file_mode=download_files)
        data = client.execute_tool(
            slug,
            user_id=user_id,
            arguments=params.get("arguments") or {},
            connected_account_id=connected_account_id,
            tool_version=params.get("tool_version"),
        )

        base_metadata: dict[str, Any] = {
            "composio_tool": slug,
            "composio_toolkit": self.toolkit or _toolkit_of(slug),
            "composio_user_id": user_id,
        }
        if connected_account_id:
            base_metadata["composio_connected_account_id"] = connected_account_id

        items = _unwrap_items(data, params.get("unwrap", False))
        if items is None:
            yield DataEnvelope(
                content_type=JSON_CONTENT_TYPE,
                data=data if isinstance(data, dict) else {"value": data},
                source_id=self.name,
                metadata=dict(base_metadata),
            )
        else:
            count = len(items)
            for index, item in enumerate(items):
                yield DataEnvelope(
                    content_type=JSON_CONTENT_TYPE,
                    data=item if isinstance(item, dict) else {"value": item},
                    source_id=self.name,
                    metadata={
                        **base_metadata,
                        "item_index": index,
                        "item_count": count,
                    },
                )

        if download_files:
            for downloaded in find_downloaded_files(data, client.download_dir):
                yield DataEnvelope(
                    content_type=guess_content_type(downloaded.path),
                    data=downloaded.path.read_bytes(),
                    source_id=self.name,
                    metadata={
                        **base_metadata,
                        "filename": downloaded.path.name,
                        "file_field": downloaded.field_path,
                    },
                )

    def validate_config(self, config: dict) -> bool:
        return _validate_config(config)

    def list_resources(self, config: dict) -> list[Resource]:
        """Browse Composio toolkits (with connection status) and — when a
        toolkit is pinned or toolkit_filter is set — their tools."""
        client = ComposioClient(config)
        scoped = (
            [self.toolkit]
            if self.toolkit
            else list(config.get("toolkit_filter") or [])
        )
        if self.toolkit:
            config = {**config, "toolkit_filter": [self.toolkit]}
        resources = _toolkit_resources(client, config)
        if scoped:
            resources.extend(_tool_resources(client, scoped))
        return resources

    def rate_limit(self) -> RateLimit | None:
        return RateLimit(requests_per_second=RATE_LIMIT_RPS)


class ComposioSink(SinkConnector):
    """Execute any Composio tool as a write action (send, create, upload...)."""

    name = "composio-sink"
    version = "0.1.0"
    input_types = ["*/*"]
    auth_type = AuthType.API_KEY
    description = "Execute any Composio tool as a write action (Slack send, Gmail send, ...)"
    docs_url = COMPOSIO_DOCS_URL

    # Set by factory subclasses to pin this connector to one toolkit.
    toolkit: str | None = None

    metadata_spec = {
        "composio_tool": MetaField(
            type=str,
            required=False,
            description="Tool slug fallback when params['tool'] is absent",
        ),
        "composio_arguments": MetaField(
            type=dict,
            required=False,
            description="Arguments merged over envelope-derived ones",
        ),
        "filename": MetaField(
            type=str, required=False, description="Upload name for binary envelopes"
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        """Execute a Composio tool with arguments derived from the envelope.

        Argument layering (later wins, shallow per-key merge):
            1. envelope payload — the data dict itself, or {data_key: payload},
               or {file_arg: <uploaded file>} for binary envelopes
            2. envelope.metadata["composio_arguments"]
            3. params["arguments"]

        Supported PushParams keys:
            tool (str): tool slug; falls back to metadata["composio_tool"]
            arguments (dict): highest-precedence argument overrides
            data_key (str): nest the envelope payload under this argument
            file_arg (str): tool argument receiving the envelope as a file upload
            user_id / connected_account_id / tool_version: as composio-source
        """
        upload_path = None
        try:
            params = params or PushParams()

            tool = params.get("tool") or envelope.metadata.get("composio_tool")
            if not tool:
                return PushResult(
                    success=False,
                    error="Missing tool: set params['tool'] or metadata['composio_tool']",
                )
            slug = _normalize_slug(tool)
            _check_toolkit_allowed(slug, config, self.toolkit)

            file_arg = params.get("file_arg")
            data_key = params.get("data_key")
            client = ComposioClient(config, file_mode=bool(file_arg))

            if file_arg:
                assert client.upload_dir is not None
                upload_path = materialize_upload_file(envelope, client.upload_dir)
                arguments: dict[str, Any] = {file_arg: str(upload_path)}
            elif envelope.stream is not None:
                return PushResult(
                    success=False,
                    error="Binary (stream) envelope requires params['file_arg']",
                )
            elif data_key:
                payload: Any = envelope.data
                if isinstance(payload, bytes):
                    try:
                        payload = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        return PushResult(
                            success=False,
                            error="Binary envelope data requires params['file_arg']",
                        )
                arguments = {data_key: payload}
            else:
                coerced = _coerce_to_dict(envelope.data)
                if coerced is None:
                    return PushResult(
                        success=False,
                        error=(
                            "Envelope data is not a JSON object; set "
                            "params['data_key'] to name the target argument "
                            "or params['file_arg'] for file uploads"
                        ),
                    )
                arguments = dict(coerced)

            meta_arguments = envelope.metadata.get("composio_arguments")
            if isinstance(meta_arguments, dict):
                arguments.update(meta_arguments)
            param_arguments = params.get("arguments")
            if isinstance(param_arguments, dict):
                arguments.update(param_arguments)

            user_id = _resolve_user(config, params.get("user_id"))
            data = client.execute_tool(
                slug,
                user_id=user_id,
                arguments=arguments,
                connected_account_id=params.get(
                    "connected_account_id", config.get("connected_account_id")
                ),
                tool_version=params.get("tool_version"),
            )

            ref = data.get("id") if isinstance(data, dict) else None
            return PushResult(
                success=True,
                ref=str(ref) if isinstance(ref, (str, int)) else slug,
                metadata={
                    "tool": slug,
                    "toolkit": self.toolkit or _toolkit_of(slug),
                    "data": data,
                },
            )
        except Exception as e:
            logger.error("Composio push failed: %s", e)
            return PushResult(success=False, error=str(e))
        finally:
            if upload_path is not None:
                try:
                    upload_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to clean up upload file %s", upload_path)

    def validate_config(self, config: dict) -> bool:
        return _validate_config(config)
