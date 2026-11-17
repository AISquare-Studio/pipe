"""Read Amazon Bedrock AgentCore policies through the control-plane API.

AgentCore has no export API. Policies are read with ``bedrock-agentcore-control``
(botocore >= 1.43.32: the Policy GA model is 1.42.60, ``enforcementMode`` 1.43.32):
``ListPolicyEngines`` -> ``ListPolicies`` (paged, up to 100 a page) ->
``GetPolicy`` only when a listed policy carries no definition. ``ListPolicies``
returns each policy's ``definition`` already, so a full read costs one call per
page, not one per policy.

A definition is a union of three members: ``cedar`` (a Cedar statement),
``policy`` (an AgentCore "Dogwood" statement) and ``policyGeneration`` (a
policy generated from natural language, referenced by its generation and
asset ids, with no statement of its own).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

from aisquare_pipe_aws.credentials import call, pages

SERVICE = "bedrock-agentcore-control"
PAGE_SIZE = 100

#: ``definition`` member -> (definition_type, content type of its text).
DEFINITION_TYPES = {
    "cedar": "text/plain",
    "policy": "text/plain",
    "policyGeneration": "application/json",
}

#: AWS's ``ResourceId`` for policy engines and policies (12-59 characters),
#: e.g. ``refund_rules-a1b2c3d4e5``.
RESOURCE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*-[a-z0-9_]{10}$"
RESOURCE_ID_MIN = 12
RESOURCE_ID_MAX = 59


def client(session: Any, region: str) -> Any:
    return session.client(SERVICE, region_name=region)


def iter_policy_engines(control: Any) -> Iterator[dict[str, Any]]:
    """Every policy engine in the client's region, every page."""
    listing = control.get_paginator("list_policy_engines").paginate(
        PaginationConfig={"PageSize": PAGE_SIZE}
    )
    for page in pages(listing, what="bedrock-agentcore:ListPolicyEngines"):
        yield from page.get("policyEngines", [])


def iter_policies(control: Any, policy_engine_id: str) -> Iterator[dict[str, Any]]:
    """Every policy of one engine, every page."""
    listing = control.get_paginator("list_policies").paginate(
        policyEngineId=policy_engine_id, PaginationConfig={"PageSize": PAGE_SIZE}
    )
    for page in pages(listing, what="bedrock-agentcore:ListPolicies"):
        yield from page.get("policies", [])


def get_policy(control: Any, policy_engine_id: str, policy_id: str) -> dict[str, Any]:
    response = call(
        control.get_policy,
        what="bedrock-agentcore:GetPolicy",
        policyEngineId=policy_engine_id,
        policyId=policy_id,
    )
    return {k: v for k, v in response.items() if k != "ResponseMetadata"}


def with_definition(control: Any, policy: Mapping[str, Any]) -> dict[str, Any]:
    """``policy`` with its definition: as listed, else from ``GetPolicy``."""
    if policy.get("definition"):
        return dict(policy)
    return get_policy(control, str(policy["policyEngineId"]), str(policy["policyId"]))


def definition_text(definition: Any) -> tuple[str, str, str]:
    """``(definition_type, content_type, text)``. A Cedar or Dogwood
    definition is its statement; a generated one is its ids as JSON."""
    if isinstance(definition, Mapping):
        for member, content_type in DEFINITION_TYPES.items():
            value = definition.get(member)
            if isinstance(value, Mapping):
                if member == "policyGeneration":
                    text = json.dumps(
                        {
                            "policyGenerationId": value.get("policyGenerationId"),
                            "policyGenerationAssetId": value.get(
                                "policyGenerationAssetId"
                            ),
                        },
                        sort_keys=True,
                    )
                else:
                    text = str(value.get("statement", ""))
                return member, content_type, text
    return (
        "unknown",
        "application/json",
        json.dumps(definition or {}, default=str, sort_keys=True),
    )


def iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None if value is None else str(value)


def engine_summary(engine: Mapping[str, Any], region: str) -> dict[str, Any]:
    return {
        "policy_engine_id": engine.get("policyEngineId"),
        "name": engine.get("name"),
        "description": engine.get("description"),
        "status": engine.get("status"),
        "policy_engine_arn": engine.get("policyEngineArn"),
        "encrypted_with_customer_key": bool(engine.get("encryptionKeyArn")),
        "created_at": iso(engine.get("createdAt")),
        "updated_at": iso(engine.get("updatedAt")),
        "region": region,
    }


def policy_record(
    policy: Mapping[str, Any], region: str, *, include_definition: bool
) -> dict[str, Any]:
    definition_type, _, text = definition_text(policy.get("definition"))
    record: dict[str, Any] = {
        "policy_id": policy.get("policyId"),
        "name": policy.get("name"),
        "description": policy.get("description"),
        "policy_engine_id": policy.get("policyEngineId"),
        "policy_arn": policy.get("policyArn"),
        "status": policy.get("status"),
        "enforcement_mode": policy.get("enforcementMode"),
        "created_at": iso(policy.get("createdAt")),
        "updated_at": iso(policy.get("updatedAt")),
        "definition_type": definition_type,
        "region": region,
    }
    reasons = policy.get("statusReasons")
    if reasons:
        record["status_reasons"] = list(reasons)
    if include_definition:
        record["definition"] = text
    return record
