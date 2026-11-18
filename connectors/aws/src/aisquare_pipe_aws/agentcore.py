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

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
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


# -- tool paging: an opaque next_token -----------------------------------------
#
# A tool call reads at most ``limit`` items and hands back a ``next_token`` for
# the rest. Each AgentCore call asks for just the items still wanted
# (``maxResults``), so a call always stops on one of AgentCore's own page
# boundaries and AgentCore's ``nextToken`` marks the spot exactly. The token
# records: the region; the ListPolicyEngines ``nextToken`` of the engine page
# the walk is in; for policies, the engine and its ListPolicies ``nextToken``;
# and the arguments that shape the walk (the engine filter), so a token can't be
# reused with other ones. It is base64url JSON, not signed: on the way back in,
# every field is checked, the region must be one the credential blob lists, and
# AgentCore checks its own tokens. A token can only move through what the
# server-side credential already allows.

#: Longest ``next_token`` a tool accepts: two AgentCore tokens of at most
#: 2048 characters each, plus the rest, as base64.
NEXT_TOKEN_MAX = 8192
#: AgentCore's own ``nextToken``: 1-2048 non-space characters.
_AWS_TOKEN_MAX = 2048
_AWS_TOKEN = re.compile(r"^\S+$")
_TOKEN_VERSION = 1
_TOKEN_KEYS = {
    "engines": {"v", "k", "r", "e"},
    "policies": {"v", "k", "r", "e", "i", "p", "f"},
}
_BAD_TOKEN = (
    "next_token is not one this tool returned for these arguments; "
    "list again without it"
)
_STALE_TOKEN = (
    "next_token is stale (its policy engine is no longer listed); list again without it"
)


@dataclass(frozen=True)
class Position:
    """Where a paged tool listing resumes."""

    region: str
    #: The ListPolicyEngines ``nextToken`` that returns the engine page the walk
    #: is in (``None``: the region's first page).
    engines_token: str | None = None
    #: Policies only: the engine to resume in (``None``: the page's first one).
    engine_id: str | None = None
    #: Policies only: the ListPolicies ``nextToken`` inside that engine
    #: (``None``: its first page).
    policies_token: str | None = None


def encode_next_token(
    kind: str, position: Position | None, *, engine_filter: str | None = None
) -> str | None:
    """The opaque ``next_token`` for ``position`` (``None`` at the end)."""
    if position is None:
        return None
    body: dict[str, Any] = {
        "v": _TOKEN_VERSION,
        "k": kind,
        "r": position.region,
        "e": position.engines_token,
    }
    if kind == "policies":
        body.update(i=position.engine_id, p=position.policies_token, f=engine_filter)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_next_token(
    token: Any,
    *,
    kind: str,
    regions: Sequence[str],
    engine_filter: str | None = None,
) -> Position | None:
    """The :class:`Position` a ``next_token`` names, or ``None`` for no token.
    Raises ``ValueError`` for anything this module didn't issue for these
    arguments, or one naming a region the credential blob doesn't list."""
    if token is None:
        return None
    if not isinstance(token, str) or not 1 <= len(token) <= NEXT_TOKEN_MAX:
        raise ValueError(_BAD_TOKEN)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body = json.loads(raw)
    except (binascii.Error, ValueError, RecursionError):
        # RecursionError: deeply nested JSON (e.g. "[[[[..."), which json
        # can't parse without recursing past the interpreter's limit.
        raise ValueError(_BAD_TOKEN) from None
    if (
        not isinstance(body, dict)
        or set(body) != _TOKEN_KEYS[kind]
        # exactly the int 1: not True or 1.0, which compare equal to it
        or type(body["v"]) is not int
        or body["v"] != _TOKEN_VERSION
        or body["k"] != kind
    ):
        raise ValueError(_BAD_TOKEN)
    region = body["r"]
    if not isinstance(region, str) or region not in regions:
        raise ValueError(
            "next_token names a region this credential does not read; "
            "list again without it"
        )
    engines_token = _aws_token(body["e"])
    if kind == "engines":
        return Position(region, engines_token)
    if body["f"] != engine_filter:
        raise ValueError(_BAD_TOKEN)
    engine_id = body["i"]
    if engine_id is not None and not is_resource_id(engine_id):
        raise ValueError(_BAD_TOKEN)
    policies_token = _aws_token(body["p"])
    if policies_token is not None and engine_id is None:
        raise ValueError(_BAD_TOKEN)
    return Position(region, engines_token, engine_id, policies_token)


def _aws_token(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > _AWS_TOKEN_MAX
        or not _AWS_TOKEN.match(value)
    ):
        raise ValueError(_BAD_TOKEN)
    return value


def is_resource_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and RESOURCE_ID_MIN <= len(value) <= RESOURCE_ID_MAX
        and re.match(RESOURCE_ID_PATTERN, value) is not None
    )


ControlFor = Callable[[str], Any]
"""A region's ``bedrock-agentcore-control`` client, built on first use."""


def _list(
    fn: Callable[..., Any], *, what: str, token: str | None, **kwargs: Any
) -> dict[str, Any]:
    if token:
        kwargs["nextToken"] = token
    response: dict[str, Any] = call(fn, what=what, **kwargs)
    return response


def engines_page(
    control_for: ControlFor,
    regions: Sequence[str],
    *,
    limit: int,
    start: Position | None,
) -> tuple[list[dict[str, Any]], Position | None]:
    """Up to ``limit`` engine summaries from ``start`` on, region by region,
    and where the next call resumes (``None``: nothing left)."""
    items: list[dict[str, Any]] = []
    first = regions.index(start.region) if start else 0
    for ri in range(first, len(regions)):
        region = regions[ri]
        control = control_for(region)
        token = start.engines_token if start and ri == first else None
        while True:
            page = _list(
                control.list_policy_engines,
                what="bedrock-agentcore:ListPolicyEngines",
                token=token,
                maxResults=min(PAGE_SIZE, limit - len(items)),
            )
            items += [
                engine_summary(e, region) for e in page.get("policyEngines") or []
            ]
            token = page.get("nextToken") or None
            if len(items) >= limit:
                if token:
                    return items, Position(region, token)
                return items, _next_region(regions, ri)
            if not token:
                break
    return items, None


def policies_page(
    control_for: ControlFor,
    regions: Sequence[str],
    *,
    limit: int,
    only: str | None,
    include_definitions: bool,
    start: Position | None,
) -> tuple[list[dict[str, Any]], Position | None]:
    """Up to ``limit`` policy records from ``start`` on (every engine, or just
    ``only``), region by region, and where the next call resumes (``None``:
    nothing left). Raises ``ValueError`` when ``start``'s engine is gone."""
    items: list[dict[str, Any]] = []
    first = regions.index(start.region) if start else 0
    for ri in range(first, len(regions)):
        region = regions[ri]
        control = control_for(region)
        resume = start if start and ri == first else None
        engines_token = resume.engines_token if resume else None
        found = False
        while not found:
            page = _list(
                control.list_policy_engines,
                what="bedrock-agentcore:ListPolicyEngines",
                token=engines_token,
                maxResults=PAGE_SIZE,
            )
            engines = [
                str(e.get("policyEngineId") or "")
                for e in page.get("policyEngines") or []
            ]
            next_engines = page.get("nextToken") or None
            begin, policies_token = 0, None
            if resume is not None and resume.engine_id is not None:
                if resume.engine_id not in engines:
                    raise ValueError(_STALE_TOKEN)
                begin = engines.index(resume.engine_id)
                policies_token = resume.policies_token
            resume = None
            for i in range(begin, len(engines)):
                engine_id = engines[i]
                if not engine_id or (only is not None and engine_id != only):
                    continue
                found = only is not None  # an engine id is unique in its region
                while True:
                    response = _list(
                        control.list_policies,
                        what="bedrock-agentcore:ListPolicies",
                        token=policies_token,
                        policyEngineId=engine_id,
                        maxResults=min(PAGE_SIZE, limit - len(items)),
                    )
                    for listed in response.get("policies") or []:
                        policy = (
                            with_definition(control, listed)
                            if include_definitions
                            else listed
                        )
                        items.append(
                            policy_record(
                                policy, region, include_definition=include_definitions
                            )
                        )
                    policies_token = response.get("nextToken") or None
                    if len(items) >= limit:
                        if policies_token:
                            return items, Position(
                                region, engines_token, engine_id, policies_token
                            )
                        if found:
                            return items, _next_region(regions, ri)
                        if i + 1 < len(engines):
                            return items, Position(
                                region, engines_token, engines[i + 1]
                            )
                        if next_engines:
                            return items, Position(region, next_engines)
                        return items, _next_region(regions, ri)
                    if not policies_token:
                        break
                if found:
                    break
            if not next_engines:
                break
            engines_token = next_engines
    return items, None


def _next_region(regions: Sequence[str], index: int) -> Position | None:
    return Position(regions[index + 1]) if index + 1 < len(regions) else None
