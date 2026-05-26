"""Canonical n8n execution payloads used by the parity harness.

Each fixture is a fully-deterministic execution dict: every timestamp is set
(no fallback to ``datetime.now``), so the shaper output is byte-stable across
runs.
"""

from __future__ import annotations

from typing import Any


def linear_finished() -> dict[str, Any]:
    """A vanilla finished execution: Start node → AI Agent node."""
    return {
        "id": 100,
        "workflowId": "wf-linear",
        "workflowData": {
            "name": "Linear Demo",
            "tags": [],
            "nodes": [
                {"name": "Start", "type": "n8n-nodes-base.start", "parameters": {}},
                {
                    "name": "Anthropic Claude",
                    "type": "@n8n/n8n-nodes-langchain.lmChatAnthropic",
                    "parameters": {
                        "model": {
                            "value": "claude-sonnet-4-5-20250929",
                            "cachedResultName": "Claude 4.5 Sonnet",
                        }
                    },
                },
            ],
        },
        "mode": "trigger",
        "finished": True,
        "status": "success",
        "startedAt": "2026-01-15T10:00:00.000Z",
        "stoppedAt": "2026-01-15T10:00:05.250Z",
        "data": {
            "resultData": {
                "runData": {
                    "Start": [
                        {
                            "startTime": 1768514400000,
                            "executionTime": 50,
                            "data": {"main": [[{"json": {"ok": True}}]]},
                            "source": [],
                        }
                    ],
                    "Anthropic Claude": [
                        {
                            "startTime": 1768514400060,
                            "executionTime": 4900,
                            "data": {
                                "main": [
                                    [
                                        {
                                            "json": {
                                                "reply": "Hello from Claude",
                                                "tokens": {"input": 12, "output": 5},
                                            }
                                        }
                                    ]
                                ]
                            },
                            "source": [{"previousNode": "Start"}],
                        }
                    ],
                }
            }
        },
    }


def branching_finished() -> dict[str, Any]:
    """A finished workflow with three nodes — Webhook → HTTP Request → Slack."""
    return {
        "id": 200,
        "workflowId": "wf-branching",
        "workflowData": {
            "name": "Branching Demo",
            "tags": [{"name": "domain:hrms"}],
            "nodes": [
                {"name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {}},
                {"name": "HTTP Request", "type": "n8n-nodes-base.httpRequest", "parameters": {}},
                {"name": "Slack", "type": "n8n-nodes-base.slack", "parameters": {}},
            ],
        },
        "mode": "webhook",
        "finished": True,
        "status": "success",
        "startedAt": "2026-01-15T11:00:00.000Z",
        "stoppedAt": "2026-01-15T11:00:01.500Z",
        "data": {
            "resultData": {
                "runData": {
                    "Webhook": [
                        {
                            "startTime": 1768518000000,
                            "executionTime": 10,
                            "data": {"main": [[{"json": {"event": "submitted"}}]]},
                            "source": [],
                        }
                    ],
                    "HTTP Request": [
                        {
                            "startTime": 1768518000020,
                            "executionTime": 400,
                            "data": {
                                "main": [
                                    [
                                        {"json": {"status": 200, "body": "ok"}},
                                    ]
                                ]
                            },
                            "source": [{"previousNode": "Webhook"}],
                        }
                    ],
                    "Slack": [
                        {
                            "startTime": 1768518000430,
                            "executionTime": 80,
                            "data": {"main": [[{"json": {"posted": True}}]]},
                            "source": [{"previousNode": "HTTP Request"}],
                        }
                    ],
                }
            }
        },
    }


def errored_finished() -> dict[str, Any]:
    """A finished but errored execution — the AI Agent node fails."""
    return {
        "id": 300,
        "workflowId": "wf-err",
        "workflowData": {
            "name": "Errored Demo",
            "tags": [],
            "nodes": [
                {"name": "OpenAI GPT", "type": "n8n-nodes-base.openAi", "parameters": {}},
            ],
        },
        "mode": "manual",
        "finished": True,
        "status": "error",
        "startedAt": "2026-01-15T12:00:00.000Z",
        "stoppedAt": "2026-01-15T12:00:02.000Z",
        "data": {
            "resultData": {
                "runData": {
                    "OpenAI GPT": [
                        {
                            "startTime": 1768521600000,
                            "executionTime": 1900,
                            "data": {},
                            "source": [],
                            "error": {"message": "rate limit exceeded"},
                        }
                    ],
                }
            }
        },
    }


def all_fixtures() -> dict[str, dict[str, Any]]:
    """Catalog of fixtures keyed by stable name. The parity test iterates
    over these and matches each against ``expected/<name>.json``."""
    return {
        "linear_finished": linear_finished(),
        "branching_finished": branching_finished(),
        "errored_finished": errored_finished(),
    }
