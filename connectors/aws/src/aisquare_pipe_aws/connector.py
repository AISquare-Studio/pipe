"""Read-only sources: Bedrock AgentCore policies and S3 objects in a customer's account.

Both read the same credential blob (:mod:`aisquare_pipe_aws.credentials`) and
reach the customer's account only through the assumed role. Every envelope
carries a version marker in its metadata (``version``), so a caller can re-read
a document when it changes:

- a policy: its ``updatedAt`` (ISO 8601);
- an S3 object: its ``VersionId`` in a versioned bucket, else its ``ETag``
  (a ``VersionId`` of ``"null"``, from a bucket whose versioning is suspended,
  counts as none).

Pull params come from callers, so none of them can name an account, role,
region, bucket or credential. Each source takes only filters that narrow what
the blob already allows.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from aisquare.pipe import AuthType, DataEnvelope, MetaField, PullParams, SourceConnector
from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_aws import agentcore, s3
from aisquare_pipe_aws.credentials import (
    AwsNotConfiguredError,
    BaseSessionFactory,
    CrossAccountConfig,
    assume_role_session,
    is_configured,
)

logger = logging.getLogger("aisquare.pipe.aws")

VERSION = "0.1.0"

#: Params a caller may send; anything else is refused.
POLICY_PARAMS = frozenset({"policy_engine_id", "updated_after", "max_policies"})
S3_PARAMS = frozenset({"prefix", "modified_after", "max_objects", "max_object_bytes"})
_MAX_ITEMS = 100_000


def _params(params: PullParams | None, allowed: frozenset[str]) -> dict[str, Any]:
    values = dict(params.params) if params is not None else {}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigValidationError(
            f"unsupported pull param(s): {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})"
        )
    return values


def _limit(value: Any, name: str, default: int, ceiling: int) -> int:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= ceiling
    ):
        raise ConfigValidationError(f"{name} must be an integer from 1 to {ceiling}")
    return value


def _after(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ConfigValidationError(
                f"{name} must be an ISO 8601 timestamp"
            ) from None
    else:
        raise ConfigValidationError(f"{name} must be an ISO 8601 timestamp")
    if moment.tzinfo is None:
        raise ConfigValidationError(
            f"{name} must carry a timezone (e.g. 2026-01-01T00:00:00Z)"
        )
    return moment


class _AwsSource(SourceConnector):
    auth_type = AuthType.CUSTOM
    #: Tests replace it; production assumes from the default credential chain.
    base_session: BaseSessionFactory | None = None

    def validate_config(self, config: dict) -> bool:
        return is_configured(config)

    def _session(self, cfg: CrossAccountConfig) -> Any:
        return assume_role_session(cfg, base_session=type(self).base_session)


class BedrockAgentCorePoliciesSource(_AwsSource):
    """One envelope per AgentCore policy, in every configured region.

    ``data`` is the policy's definition text (a Cedar or Dogwood statement; a
    generated policy's generation and asset ids as JSON). ``metadata`` holds
    the policy's name, ids, ARN, status, enforcement mode, timestamps, its
    engine, the region and ``version`` (``updatedAt``).
    """

    name = "bedrock-agentcore-policies-source"
    version = VERSION
    output_types = ["text/plain", "application/json"]  # noqa: RUF012 — framework contract: class-level spec, never mutated
    description = (
        "Amazon Bedrock AgentCore policies (Cedar, Dogwood and generated) from a customer's AWS "
        "account, read through a cross-account IAM role"
    )
    docs_url = (
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html"
    )
    metadata_spec = {  # noqa: RUF012 — framework contract: class-level spec, never mutated
        "policy_id": MetaField(str, required=True, description="AgentCore policy id"),
        "policy_name": MetaField(str, required=True, description="Policy name"),
        "policy_arn": MetaField(str, required=True, description="Policy ARN"),
        "policy_engine_id": MetaField(
            str, required=True, description="Its policy engine"
        ),
        "policy_engine_name": MetaField(str, description="Its policy engine's name"),
        "definition_type": MetaField(
            str, required=True, description="cedar | policy | policyGeneration"
        ),
        "status": MetaField(str, description="CREATING | ACTIVE | UPDATING | ... "),
        "enforcement_mode": MetaField(str, description="ACTIVE | LOG_ONLY"),
        "created_at": MetaField(str, description="ISO 8601"),
        "updated_at": MetaField(str, required=True, description="ISO 8601"),
        "region": MetaField(str, required=True, description="AWS region"),
        "account_id": MetaField(
            str, required=True, description="The customer's AWS account"
        ),
        "version": MetaField(
            str, required=True, description="Version marker: updatedAt"
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        cfg = CrossAccountConfig.from_config(config)
        values = _params(params, POLICY_PARAMS)
        engine_filter = values.get("policy_engine_id")
        if engine_filter is not None and (
            not isinstance(engine_filter, str)
            or len(engine_filter) > agentcore.RESOURCE_ID_MAX
            or not re.match(agentcore.RESOURCE_ID_PATTERN, engine_filter)
        ):
            raise ConfigValidationError("policy_engine_id is not a policy engine id")
        updated_after = _after(values.get("updated_after"), "updated_after")
        budget = _limit(
            values.get("max_policies"), "max_policies", _MAX_ITEMS, _MAX_ITEMS
        )
        session = self._session(cfg)
        emitted = 0
        for region in cfg.regions:
            control = agentcore.client(session, region)
            for engine in agentcore.iter_policy_engines(control):
                engine_id = str(engine.get("policyEngineId", ""))
                if engine_filter is not None and engine_id != engine_filter:
                    continue
                for listed in agentcore.iter_policies(control, engine_id):
                    updated = listed.get("updatedAt")
                    if (
                        updated_after is not None
                        and isinstance(updated, datetime)
                        and updated <= updated_after
                    ):
                        continue
                    policy = agentcore.with_definition(control, listed)
                    yield self._envelope(cfg, region, engine, policy)
                    emitted += 1
                    if emitted >= budget:
                        return

    def _envelope(
        self, cfg: CrossAccountConfig, region: str, engine: dict, policy: dict
    ) -> DataEnvelope:
        definition_type, content_type, text = agentcore.definition_text(
            policy.get("definition")
        )
        updated_at = agentcore.iso(policy.get("updatedAt")) or ""
        metadata: dict[str, Any] = {
            "policy_id": policy.get("policyId"),
            "policy_name": policy.get("name"),
            "description": policy.get("description"),
            "policy_arn": policy.get("policyArn"),
            "policy_engine_id": policy.get("policyEngineId"),
            "policy_engine_name": engine.get("name"),
            "policy_engine_arn": engine.get("policyEngineArn"),
            "definition_type": definition_type,
            "status": policy.get("status"),
            "enforcement_mode": policy.get("enforcementMode"),
            "created_at": agentcore.iso(policy.get("createdAt")),
            "updated_at": updated_at,
            "region": region,
            "account_id": cfg.account_id,
            "version": updated_at,
        }
        return DataEnvelope(
            content_type=content_type,
            data=text,
            source_id=str(policy.get("policyArn") or policy.get("policyId")),
            metadata={k: v for k, v in metadata.items() if v is not None},
        )


class S3Source(_AwsSource):
    """One envelope per object under the configured bucket and prefix.

    ``data`` is the object's bytes (objects over ``max_object_bytes``, 25 MiB
    by default, are skipped and logged). ``metadata`` holds the bucket, key,
    ``etag``, ``last_modified``, ``version_id`` and ``version`` (the version
    id when the bucket is versioned, else the ETag; a ``"null"`` version id
    counts as none).
    """

    name = "s3-source"
    version = VERSION
    output_types = ["*/*"]  # noqa: RUF012 — framework contract: class-level spec, never mutated
    description = (
        "Objects from one bucket and prefix in a customer's AWS account, read through a "
        "cross-account IAM role"
    )
    docs_url = "https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html"
    metadata_spec = {  # noqa: RUF012 — framework contract: class-level spec, never mutated
        "bucket": MetaField(str, required=True, description="S3 bucket"),
        "key": MetaField(str, required=True, description="Object key"),
        "etag": MetaField(str, required=True, description="Entity tag"),
        "last_modified": MetaField(str, required=True, description="ISO 8601"),
        "version_id": MetaField(str, description="Object version (versioned buckets)"),
        "size": MetaField(int, description="Bytes"),
        "account_id": MetaField(
            str, required=True, description="The customer's AWS account"
        ),
        "version": MetaField(
            str, required=True, description="Version marker: VersionId, else ETag"
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        cfg = CrossAccountConfig.from_config(config)
        if cfg.s3 is None:
            raise AwsNotConfiguredError(
                "S3 is not configured: the credential blob has no s3.bucket"
            )
        location = cfg.s3
        values = _params(params, S3_PARAMS)
        try:
            prefix = s3.scoped_prefix(location, values.get("prefix"))
        except ValueError as e:
            raise ConfigValidationError(str(e)) from None
        modified_after = _after(values.get("modified_after"), "modified_after")
        budget = _limit(
            values.get("max_objects"), "max_objects", _MAX_ITEMS, _MAX_ITEMS
        )
        max_bytes = _limit(
            values.get("max_object_bytes"),
            "max_object_bytes",
            s3.DEFAULT_MAX_OBJECT_BYTES,
            s3.DEFAULT_MAX_OBJECT_BYTES,
        )
        client = s3.client(self._session(cfg), location)
        emitted = 0
        for obj in s3.iter_objects(client, location, prefix):
            modified = obj.get("LastModified")
            if (
                modified_after is not None
                and isinstance(modified, datetime)
                and modified <= modified_after
            ):
                continue
            key = str(obj["Key"])
            size = obj.get("Size")
            if isinstance(size, int) and size > max_bytes:
                logger.info(
                    "s3-source: skipped an object of %d bytes (limit %d)",
                    size,
                    max_bytes,
                )
                continue
            data, meta = s3.get_object(client, location, key, max_bytes=max_bytes)
            metadata = {**meta, "account_id": cfg.account_id}
            content_type = str(metadata.pop("content_type"))
            yield DataEnvelope(
                content_type=content_type,
                data=data,
                source_id=f"s3://{location.bucket}/{key}",
                metadata={k: v for k, v in metadata.items() if v is not None},
            )
            emitted += 1
            if emitted >= budget:
                return
