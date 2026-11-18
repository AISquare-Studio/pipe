"""Read-only toolkits over a customer's AWS account: ``bedrock_agentcore`` and ``s3``.

Both take the same server-side credential blob as their config
(:mod:`aisquare_pipe_aws.credentials`); no tool argument names an account,
role, region, bucket or credential. Tools that span regions read every region
the blob lists.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from aisquare.pipe import (
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_CONNECTED,
    ERROR_NOT_FOUND,
    PRIMARY_TAG,
    AuthType,
    DataEnvelope,
    ToolError,
    Toolkit,
    ToolResult,
    ToolSpec,
)

from aisquare_pipe_aws import agentcore, s3
from aisquare_pipe_aws.credentials import (
    AwsAccessError,
    AwsNotConfiguredError,
    BaseSessionFactory,
    CrossAccountConfig,
    cached_session,
    is_configured,
)

VERSION = "0.1.0"
_INLINE_TEXT_BYTES = 512 * 1024
_TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
)


def _bounded(args: Mapping[str, Any], name: str, default: int, maximum: int) -> int:
    """The framework checks types, not bounds: clamp here."""
    value = args.get(name, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _resource_id(args: Mapping[str, Any], name: str, *, required: bool) -> str | None:
    value = args.get(name)
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not agentcore.RESOURCE_ID_MIN <= len(value) <= agentcore.RESOURCE_ID_MAX
        or not re.match(agentcore.RESOURCE_ID_PATTERN, value)
    ):
        raise ValueError(f"{name} is not an AgentCore id")
    return value


def _page(items: list[dict[str, Any]], next_token: str | None) -> dict[str, Any]:
    return {
        "items": items,
        "has_more": next_token is not None,
        "next_token": next_token,
    }


def _schema(
    properties: dict[str, Any], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _limit(description: str, default: int, maximum: int) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": maximum,
        "default": default,
        "description": description,
    }


_NEXT_TOKEN = {
    "type": "string",
    "minLength": 1,
    "maxLength": agentcore.NEXT_TOKEN_MAX,
    "description": "next_token from the previous call, to continue the listing.",
}

_RESOURCE_ID = {
    "type": "string",
    "pattern": agentcore.RESOURCE_ID_PATTERN,
    "minLength": agentcore.RESOURCE_ID_MIN,
    "maxLength": agentcore.RESOURCE_ID_MAX,
}


class _AwsToolkit(Toolkit):
    auth_type = AuthType.CUSTOM
    #: Tests replace it; production assumes from the default credential chain.
    base_session: BaseSessionFactory | None = None
    _handlers: ClassVar[Mapping[str, str]] = {}

    def validate_config(self, config: dict[str, Any]) -> bool:
        return is_configured(config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        handler: Callable[[CrossAccountConfig, dict[str, Any]], ToolResult] = getattr(
            self, self._handlers[tool.slug]
        )
        try:
            cfg = CrossAccountConfig.from_config(config)
            return handler(cfg, arguments)
        except AwsNotConfiguredError as e:
            raise ToolError(str(e), code=ERROR_NOT_CONNECTED) from None
        except AwsAccessError as e:
            raise ToolError(str(e), code=e.code) from None
        except ValueError as e:
            raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None

    def _session(self, cfg: CrossAccountConfig) -> Any:
        return cached_session(cfg, base_session=type(self).base_session)


class BedrockAgentCoreToolkit(_AwsToolkit):
    """Amazon Bedrock AgentCore policies: list engines, list and read policies."""

    name = "bedrock_agentcore"
    version = VERSION
    display_name = "Amazon Bedrock AgentCore"
    description = (
        "Read the Cedar and natural-language policies of Amazon Bedrock AgentCore policy engines "
        "in a customer's AWS account (read-only, cross-account role)."
    )
    category = "data"
    keywords = (
        "agentcore",
        "bedrock",
        "cedar",
        "policy engine",
        "guardrails",
        "aws",
        "policies",
    )
    examples = (
        "list our AgentCore policy engines",
        "show the Cedar policies in our AgentCore policy engine",
        "which AgentCore policies are in log-only mode?",
    )
    docs_url = (
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html"
    )
    _handlers: ClassVar[Mapping[str, str]] = {
        "BEDROCK_AGENTCORE_LIST_POLICY_ENGINES": "_list_engines",
        "BEDROCK_AGENTCORE_LIST_POLICIES": "_list_policies",
        "BEDROCK_AGENTCORE_GET_POLICY": "_get_policy",
    }

    _TOOLS = (
        ToolSpec(
            "BEDROCK_AGENTCORE_LIST_POLICY_ENGINES",
            "List the Amazon Bedrock AgentCore policy engines in every configured region of the "
            "customer's AWS account: id, name, status, ARN, region, timestamps and whether a "
            "customer-managed KMS key encrypts it. Use an engine's policy_engine_id with "
            "BEDROCK_AGENTCORE_LIST_POLICIES. While has_more is true, pass next_token back "
            "for the rest.",
            _schema(
                {
                    "limit": _limit("Most engines to return (default 100).", 100, 500),
                    "next_token": _NEXT_TOKEN,
                }
            ),
            read_only=True,
            tags=("agentcore", "policy engine", "list", "aws", "bedrock"),
        ),
        ToolSpec(
            "BEDROCK_AGENTCORE_LIST_POLICIES",
            "List Amazon Bedrock AgentCore policies (Cedar, Dogwood and generated) with their "
            "name, status, enforcement mode (ACTIVE or LOG_ONLY), ARN, region, createdAt and "
            "updatedAt, across every policy engine or just one (policy_engine_id). "
            "include_definitions adds each policy's statement text. updatedAt is the version "
            "marker: re-read a policy when it changes. While has_more is true, pass next_token "
            "back (with the same policy_engine_id) for the rest.",
            _schema(
                {
                    "policy_engine_id": {
                        **_RESOURCE_ID,
                        "description": "Only this engine's policies (default: every engine).",
                    },
                    "include_definitions": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include each policy's definition text.",
                    },
                    "limit": _limit("Most policies to return (default 100).", 100, 500),
                    "next_token": _NEXT_TOKEN,
                }
            ),
            read_only=True,
            tags=(
                "agentcore",
                "policies",
                "cedar",
                "list",
                "export",
                "rules",
                PRIMARY_TAG,
            ),
        ),
        ToolSpec(
            "BEDROCK_AGENTCORE_GET_POLICY",
            "Read one Amazon Bedrock AgentCore policy by engine id and policy id: its definition "
            "text (a Cedar or Dogwood statement, or a generated policy's ids), status, "
            "enforcement mode, ARN and timestamps.",
            _schema(
                {
                    "policy_engine_id": {
                        **_RESOURCE_ID,
                        "description": "The policy engine id.",
                    },
                    "policy_id": {**_RESOURCE_ID, "description": "The policy id."},
                },
                ("policy_engine_id", "policy_id"),
            ),
            read_only=True,
            tags=(
                "agentcore",
                "policy",
                "cedar",
                "get",
                "read",
                "definition",
                PRIMARY_TAG,
            ),
        ),
    )

    def tools(self) -> list[ToolSpec]:
        return list(self._TOOLS)

    def _list_engines(
        self, cfg: CrossAccountConfig, args: dict[str, Any]
    ) -> ToolResult:
        limit = _bounded(args, "limit", 100, 500)
        start = agentcore.decode_next_token(
            args.get("next_token"), kind="engines", regions=cfg.regions
        )
        items, after = agentcore.engines_page(
            self._controls(cfg), cfg.regions, limit=limit, start=start
        )
        return ToolResult.ok(
            _page(items, agentcore.encode_next_token("engines", after))
        )

    def _list_policies(
        self, cfg: CrossAccountConfig, args: dict[str, Any]
    ) -> ToolResult:
        limit = _bounded(args, "limit", 100, 500)
        only = _resource_id(args, "policy_engine_id", required=False)
        include = bool(args.get("include_definitions", False))
        start = agentcore.decode_next_token(
            args.get("next_token"),
            kind="policies",
            regions=cfg.regions,
            engine_filter=only,
        )
        items, after = agentcore.policies_page(
            self._controls(cfg),
            cfg.regions,
            limit=limit,
            only=only,
            include_definitions=include,
            start=start,
        )
        token = agentcore.encode_next_token("policies", after, engine_filter=only)
        return ToolResult.ok(_page(items, token))

    def _controls(self, cfg: CrossAccountConfig) -> agentcore.ControlFor:
        """Each region's AgentCore client, on one assumed-role session."""
        session = self._session(cfg)
        return lambda region: agentcore.client(session, region)

    def _get_policy(self, cfg: CrossAccountConfig, args: dict[str, Any]) -> ToolResult:
        engine_id = str(_resource_id(args, "policy_engine_id", required=True))
        policy_id = str(_resource_id(args, "policy_id", required=True))
        session = self._session(cfg)
        for region in cfg.regions:
            control = agentcore.client(session, region)
            try:
                policy = agentcore.get_policy(control, engine_id, policy_id)
            except AwsAccessError as e:
                if e.code == ERROR_NOT_FOUND:
                    continue
                raise
            return ToolResult.ok(
                agentcore.policy_record(policy, region, include_definition=True)
            )
        raise ToolError("no such policy in any configured region", code=ERROR_NOT_FOUND)


class S3Toolkit(_AwsToolkit):
    """Amazon S3 objects in the one bucket and prefix the credential blob names."""

    name = "s3"
    version = VERSION
    display_name = "Amazon S3"
    description = (
        "List and read objects in the customer's configured Amazon S3 bucket and prefix "
        "(read-only, cross-account role), with ETag, LastModified and VersionId."
    )
    category = "files"
    keywords = ("s3", "aws", "bucket", "objects", "files", "documents")
    examples = (
        "list the files in our S3 policy bucket",
        "read the rule book document from S3",
        "has the S3 policy file changed?",
    )
    docs_url = "https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html"
    _handlers: ClassVar[Mapping[str, str]] = {
        "S3_LIST_OBJECTS": "_list_objects",
        "S3_GET_OBJECT": "_get_object",
        "S3_HEAD_OBJECT": "_head_object",
    }

    _KEY: ClassVar[dict[str, Any]] = {
        "type": "string",
        "minLength": 1,
        "maxLength": s3.MAX_KEY,
    }
    _VERSION_ID: ClassVar[dict[str, Any]] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 1024,
        "description": "A specific object version (versioned buckets).",
    }

    _TOOLS = (
        ToolSpec(
            "S3_LIST_OBJECTS",
            "List objects (files) in the configured Amazon S3 bucket, under its configured prefix "
            "or a narrower prefix inside it, one page at a time: key, size, etag, last_modified. "
            "Pass next_continuation_token as continuation_token for the next page.",
            _schema(
                {
                    "prefix": {
                        "type": "string",
                        "maxLength": s3.MAX_KEY,
                        "description": "A key prefix inside the configured prefix (default: it).",
                    },
                    "limit": _limit("Most objects per page (default 100).", 100, 1000),
                    "continuation_token": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4096,
                        "description": "next_continuation_token from the previous page.",
                    },
                }
            ),
            read_only=True,
            tags=("s3", "list", "files", "objects", "bucket", "browse", PRIMARY_TAG),
        ),
        ToolSpec(
            "S3_GET_OBJECT",
            "Read one object from the configured Amazon S3 bucket by key (and optionally "
            "version_id). Text up to 512 KiB comes back as text; anything else as a file. Returns "
            "its etag, last_modified and version_id, the version markers to re-read on change.",
            _schema(
                {
                    "key": {**_KEY, "description": "The object key."},
                    "version_id": _VERSION_ID,
                },
                ("key",),
            ),
            read_only=True,
            tags=("s3", "read", "download", "file", "document", "get", PRIMARY_TAG),
        ),
        ToolSpec(
            "S3_HEAD_OBJECT",
            "Check one object in the configured Amazon S3 bucket without downloading it: size, "
            "content type, etag, last_modified and version_id. Use it to see whether a file "
            "changed since it was last read.",
            _schema(
                {
                    "key": {**_KEY, "description": "The object key."},
                    "version_id": _VERSION_ID,
                },
                ("key",),
            ),
            read_only=True,
            tags=("s3", "metadata", "changed", "etag", "version", "head"),
        ),
    )

    def tools(self) -> list[ToolSpec]:
        return list(self._TOOLS)

    def validate_config(self, config: dict[str, Any]) -> bool:
        if not is_configured(config):
            return False
        return CrossAccountConfig.from_config(config).s3 is not None

    @staticmethod
    def _location(cfg: CrossAccountConfig) -> s3.S3Location:
        if cfg.s3 is None:
            raise AwsNotConfiguredError(
                "S3 is not configured: the credential blob has no s3.bucket"
            )
        return cfg.s3

    def _list_objects(
        self, cfg: CrossAccountConfig, args: dict[str, Any]
    ) -> ToolResult:
        location = self._location(cfg)
        prefix = s3.scoped_prefix(location, args.get("prefix"))
        client = s3.client(self._session(cfg), location)
        page = s3.list_page(
            client,
            location,
            prefix,
            limit=_bounded(args, "limit", 100, 1000),
            continuation_token=args.get("continuation_token"),
        )
        return ToolResult.ok(page)

    def _get_object(self, cfg: CrossAccountConfig, args: dict[str, Any]) -> ToolResult:
        location = self._location(cfg)
        key = s3.check_key(location, args.get("key"))
        client = s3.client(self._session(cfg), location)
        data, meta = s3.get_object(
            client, location, key, version_id=args.get("version_id")
        )
        content_type = str(meta["content_type"])
        if len(data) <= _INLINE_TEXT_BYTES and content_type.startswith(_TEXT_TYPES):
            try:
                return ToolResult.ok({**meta, "text": data.decode("utf-8")})
            except UnicodeDecodeError:
                pass
        envelope = DataEnvelope(
            content_type=content_type,
            data=data,
            source_id=f"s3://{location.bucket}/{key}",
            metadata={k: v for k, v in meta.items() if v is not None},
        )
        return ToolResult.ok({**meta, "file": True}, files=[envelope])

    def _head_object(self, cfg: CrossAccountConfig, args: dict[str, Any]) -> ToolResult:
        location = self._location(cfg)
        key = s3.check_key(location, args.get("key"))
        client = s3.client(self._session(cfg), location)
        return ToolResult.ok(
            s3.head_object(client, location, key, version_id=args.get("version_id"))
        )
