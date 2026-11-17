"""bedrock-agentcore-policies-source and the bedrock_agentcore toolkit.

moto does not model bedrock-agentcore-control, so each customer client the
code builds gets a botocore ``Stubber`` with that region's pages queued. STS
is moto, so the assumed role and its credentials are real moto ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aisquare.pipe import PullParams
from aisquare.pipe.errors import ConfigValidationError
from botocore.stub import Stubber

from aisquare_pipe_aws import agentcore
from aisquare_pipe_aws.connector import BedrockAgentCorePoliciesSource
from aisquare_pipe_aws.credentials import AwsNotConfiguredError
from aisquare_pipe_aws.toolkit import BedrockAgentCoreToolkit
from tests.conftest import (
    AMBIENT_KEY,
    CUSTOMER_ACCOUNT,
    EXTERNAL_ID,
    access_key_of,
    blob,
)

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
CEDAR = 'permit(principal, action == AgentCore::Action::"refund", resource) when { context.amount < 100 };'


def eid(i: int) -> str:
    """A policy engine id in AWS's shape (``<name>-<10 chars>``)."""
    return f"engine{i}-{i:010d}"


def pid(engine_id: str, j: int) -> str:
    return f"{engine_id.split('-')[0]}pol{j}-{j:010d}"


def _engine(i: int, region: str = "us-east-1") -> dict[str, Any]:
    return {
        "policyEngineId": eid(i),
        "name": f"engine_{i}",
        "createdAt": T0,
        "updatedAt": T0,
        "policyEngineArn": f"arn:aws:bedrock-agentcore:{region}:{CUSTOMER_ACCOUNT}:policy-engine/{eid(i)}",
        "status": "ACTIVE",
        "statusReasons": [],
    }


def _policy(
    engine_id: str,
    j: int,
    *,
    definition: dict | None = None,
    updated: datetime | None = None,
) -> dict[str, Any]:
    return {
        "policyId": pid(engine_id, j),
        "name": f"policy_{j}",
        "policyEngineId": engine_id,
        "createdAt": T0,
        "updatedAt": updated or T0 + timedelta(minutes=j),
        "policyArn": (
            f"arn:aws:bedrock-agentcore:us-east-1:{CUSTOMER_ACCOUNT}:policy-engine/{engine_id}"
            f"/policy/{pid(engine_id, j)}"
        ),
        "status": "ACTIVE",
        "enforcementMode": "LOG_ONLY" if j % 2 else "ACTIVE",
        "definition": definition or {"cedar": {"statement": CEDAR}},
        "statusReasons": [],
    }


class Control:
    """Queue AgentCore pages per region; every client the code builds is checked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.queues: dict[str, list[tuple[str, dict, dict | None]]] = {}
        self.clients: list[tuple[str, Any, Stubber]] = []
        real = agentcore.client

        def build(session: Any, region: str) -> Any:
            # One stubbed client per region, so calls spread over several
            # tool calls drain one queue in order.
            for known_region, client, _ in self.clients:
                if known_region == region:
                    return client
            client = real(session, region)
            stub = Stubber(client)
            for method, response, expected in self.queues.get(region, []):
                stub.add_response(method, response, expected)
            stub.activate()
            self.clients.append((region, client, stub))
            return client

        monkeypatch.setattr(agentcore, "client", build)

    def add(
        self, region: str, method: str, response: dict, expected: dict | None = None
    ) -> None:
        self.queues.setdefault(region, []).append((method, response, expected))

    def engines(self, region: str, pages: list[list[dict]]) -> None:
        for n, page in enumerate(pages):
            response: dict[str, Any] = {"policyEngines": page}
            expected: dict[str, Any] = {"maxResults": agentcore.PAGE_SIZE}
            if n:
                expected["nextToken"] = f"engines-{n}"
            if n + 1 < len(pages):
                response["nextToken"] = f"engines-{n + 1}"
            self.add(region, "list_policy_engines", response, expected)

    def policies(self, region: str, engine_id: str, pages: list[list[dict]]) -> None:
        for n, page in enumerate(pages):
            response: dict[str, Any] = {"policies": page}
            expected: dict[str, Any] = {
                "policyEngineId": engine_id,
                "maxResults": agentcore.PAGE_SIZE,
            }
            if n:
                expected["nextToken"] = f"{engine_id}-{n}"
            if n + 1 < len(pages):
                response["nextToken"] = f"{engine_id}-{n + 1}"
            self.add(region, "list_policies", response, expected)

    def drained(self) -> None:
        for _, _, stub in self.clients:
            stub.assert_no_pending_responses()


@pytest.fixture
def control(monkeypatch, aws, assume_calls):
    return Control(monkeypatch)


def _pull(config: dict, **params: Any) -> list:
    return list(
        BedrockAgentCorePoliciesSource().pull(
            config, PullParams(params) if params else None
        )
    )


def test_every_policy_of_every_engine_on_every_page_in_every_region(
    control, assume_calls
):
    # us-east-1: two pages of engines; engine0 has 205 policies over three pages.
    # The calls interleave: engine page 1, engine0's policies, engine page 2, ...
    control.add(
        "us-east-1",
        "list_policy_engines",
        {"policyEngines": [_engine(0)], "nextToken": "engines-1"},
        {"maxResults": agentcore.PAGE_SIZE},
    )
    control.policies(
        "us-east-1",
        eid(0),
        [
            [_policy(eid(0), j) for j in range(100)],
            [_policy(eid(0), j) for j in range(100, 200)],
            [_policy(eid(0), j) for j in range(200, 205)],
        ],
    )
    control.add(
        "us-east-1",
        "list_policy_engines",
        {"policyEngines": [_engine(1)]},
        {"maxResults": agentcore.PAGE_SIZE, "nextToken": "engines-1"},
    )
    control.policies("us-east-1", eid(1), [[_policy(eid(1), 0)]])
    # eu-west-1: one engine, one policy.
    control.engines("eu-west-1", [[_engine(7, "eu-west-1")]])
    control.policies("eu-west-1", eid(7), [[_policy(eid(7), 0)]])

    envelopes = _pull(blob(regions=["us-east-1", "eu-west-1"]))

    assert len(envelopes) == 205 + 1 + 1
    assert len({e.metadata["policy_id"] for e in envelopes}) == 207
    assert [e.metadata["region"] for e in envelopes][-1] == "eu-west-1"
    control.drained()
    # One AssumeRole, with the external id; every AgentCore client signs as the customer role.
    assert len(assume_calls) == 1 and assume_calls[0]["ExternalId"] == EXTERNAL_ID
    for _, client, _ in control.clients:
        key = access_key_of(client)
        assert key != AMBIENT_KEY and key.startswith("ASIA")


def test_an_envelope_is_the_definition_text_with_its_metadata_and_version(control):
    updated = T0 + timedelta(days=3)
    control.engines("us-east-1", [[_engine(0)]])
    control.policies(
        "us-east-1",
        eid(0),
        [
            [
                _policy(eid(0), 1, updated=updated),
                _policy(
                    eid(0),
                    2,
                    definition={
                        "policy": {"statement": "forbid refunds over 1000 on weekends"}
                    },
                ),
                _policy(
                    eid(0),
                    3,
                    definition={
                        "policyGeneration": {
                            "policyGenerationId": "gen1-0123456789",
                            "policyGenerationAssetId": "asset1-0123456789",
                        }
                    },
                ),
            ]
        ],
    )
    cedar, dogwood, generated = _pull(blob())

    assert cedar.content_type == "text/plain" and cedar.data == CEDAR
    assert cedar.source_id.endswith(f"/policy/{pid(eid(0), 1)}")
    meta = cedar.metadata
    assert meta["definition_type"] == "cedar"
    assert (
        meta["policy_name"] == "policy_1" and meta["policy_engine_name"] == "engine_0"
    )
    assert meta["status"] == "ACTIVE" and meta["enforcement_mode"] == "LOG_ONLY"
    assert meta["updated_at"] == updated.isoformat() == meta["version"]
    assert meta["account_id"] == CUSTOMER_ACCOUNT and meta["region"] == "us-east-1"
    assert dogwood.metadata["definition_type"] == "policy" and dogwood.data.startswith(
        "forbid"
    )
    assert generated.content_type == "application/json"
    assert generated.metadata["definition_type"] == "policyGeneration"
    assert '"policyGenerationId": "gen1-0123456789"' in generated.data


def test_updated_after_returns_only_policies_changed_since(control):
    control.engines("us-east-1", [[_engine(0)]])
    control.policies("us-east-1", eid(0), [[_policy(eid(0), j) for j in range(5)]])
    since = (T0 + timedelta(minutes=2)).isoformat()
    envelopes = _pull(blob(), updated_after=since)
    assert [e.metadata["policy_id"] for e in envelopes] == [
        pid(eid(0), 3),
        pid(eid(0), 4),
    ]


def test_policy_engine_id_narrows_to_one_engine(control):
    control.engines("us-east-1", [[_engine(0), _engine(1)]])
    control.policies("us-east-1", eid(1), [[_policy(eid(1), 0)]])
    envelopes = _pull(blob(), policy_engine_id=eid(1))
    assert [e.metadata["policy_engine_id"] for e in envelopes] == [eid(1)]
    control.drained()


def test_a_listed_policy_without_its_definition_is_read_with_get_policy():
    class Fake:
        def __init__(self) -> None:
            self.got: list[tuple[str, str]] = []

        def get_policy(self, policyEngineId: str, policyId: str) -> dict:
            self.got.append((policyEngineId, policyId))
            return {**_policy(policyEngineId, 9), "ResponseMetadata": {}}

    fake = Fake()
    listed = {k: v for k, v in _policy(eid(0), 9).items() if k != "definition"}
    full = agentcore.with_definition(fake, listed)
    assert fake.got == [(eid(0), pid(eid(0), 9))]
    assert (
        full["definition"]["cedar"]["statement"] == CEDAR
        and "ResponseMetadata" not in full
    )


# -- refusals ----------------------------------------------------------------------


def test_pull_without_a_blob_fails_closed_on_first_read():
    iterator = BedrockAgentCorePoliciesSource().pull({})
    with pytest.raises(AwsNotConfiguredError):
        next(iterator)


@pytest.mark.parametrize(
    "params",
    [
        {"role_arn": "arn:aws:iam::999999999999:role/x"},
        {"region": "eu-west-1"},
        {"regions": ["eu-west-1"]},
        {"external_id": "mine"},
        {"aws_access_key_id": "AKIA..."},
        {"policy_engine_id": "../etc"},
        {"policy_engine_id": "engine0"},  # not AWS's id shape
        {"updated_after": "yesterday"},
        {"updated_after": "2026-01-01T00:00:00"},  # no timezone
        {"max_policies": 0},
    ],
)
def test_a_caller_cannot_override_a_server_key_or_send_a_bad_filter(
    params, aws, assume_calls
):
    with pytest.raises(ConfigValidationError):
        _pull(blob(), **params)
    assert assume_calls == []  # refused before anything reaches AWS


# -- the toolkit -------------------------------------------------------------------


def test_toolkit_lists_engines_and_policies_and_reads_one(control):
    control.engines("us-east-1", [[_engine(0)]])
    control.policies("us-east-1", eid(0), [[_policy(eid(0), 1), _policy(eid(0), 2)]])
    control.engines("us-east-1", [[_engine(0)]])
    control.add(
        "us-east-1",
        "get_policy",
        {k: v for k, v in _policy(eid(0), 2).items()},
        {"policyEngineId": eid(0), "policyId": pid(eid(0), 2)},
    )
    kit = BedrockAgentCoreToolkit()
    config = blob()

    listed = kit.call_tool("BEDROCK_AGENTCORE_LIST_POLICIES", {}, config)
    assert listed.successful, listed.error
    assert [p["policy_id"] for p in listed.data["items"]] == [
        pid(eid(0), 1),
        pid(eid(0), 2),
    ]
    assert "definition" not in listed.data["items"][0]  # only with include_definitions

    engines = kit.call_tool("BEDROCK_AGENTCORE_LIST_POLICY_ENGINES", {}, config)
    assert engines.successful and engines.data["items"][0]["policy_engine_id"] == eid(0)

    got = kit.call_tool(
        "BEDROCK_AGENTCORE_GET_POLICY",
        {"policy_engine_id": eid(0), "policy_id": pid(eid(0), 2)},
        config,
    )
    assert got.successful, got.error
    assert got.data["definition"] == CEDAR and got.data["definition_type"] == "cedar"
    control.drained()


def test_toolkit_paging_follows_next_token_to_the_limit(control):
    control.engines("us-east-1", [[_engine(0)]])
    control.policies(
        "us-east-1",
        eid(0),
        [
            [_policy(eid(0), j) for j in range(100)],
            [_policy(eid(0), j) for j in range(100, 130)],
        ],
    )
    result = BedrockAgentCoreToolkit().call_tool(
        "BEDROCK_AGENTCORE_LIST_POLICIES",
        {"limit": 120, "include_definitions": True},
        blob(),
    )
    assert result.successful, result.error
    assert len(result.data["items"]) == 120 and result.data["has_more"] is True
    assert result.data["items"][0]["definition"] == CEDAR


def test_toolkit_without_a_blob_is_not_connected():
    result = BedrockAgentCoreToolkit().call_tool(
        "BEDROCK_AGENTCORE_LIST_POLICIES", {}, {}
    )
    assert not result.successful and result.error_code == "not_connected"


@pytest.mark.parametrize(
    "arguments",
    [
        {"region": "eu-west-1"},
        {"role_arn": "arn:aws:iam::999999999999:role/x"},
        {"external_id": "x"},
        {"limit": 10_000},
        {"policy_engine_id": "../x"},
    ],
)
def test_toolkit_refuses_server_keys_and_bad_arguments(arguments, aws, assume_calls):
    result = BedrockAgentCoreToolkit().call_tool(
        "BEDROCK_AGENTCORE_LIST_POLICIES", arguments, blob()
    )
    assert not result.successful and result.error_code == "invalid_arguments", (
        result.error
    )
    assert assume_calls == []
