"""Canned execution payloads for n8n connector tests."""

from __future__ import annotations

from typing import Any


def make_workflow_def(
    *,
    workflow_id: str = "wf-1",
    name: str = "Demo Workflow",
    node_names: list[str] | None = None,
) -> dict[str, Any]:
    """Build an n8n-shaped workflow definition payload."""
    node_names = node_names or ["Start", "AI Agent"]
    return {
        "id": workflow_id,
        "name": name,
        "nodes": [
            {"name": n, "type": "n8n-nodes-base.noOp", "parameters": {}}
            for n in node_names
        ],
        "tags": [],
    }


def make_execution(
    *,
    execution_id: int,
    workflow_id: str = "wf-1",
    workflow_name: str = "Demo Workflow",
    finished: bool = True,
    status: str = "success",
    nodes: dict[str, list[dict[str, Any]]] | None = None,
    mode: str = "manual",
    started_at: str = "2024-01-01T00:00:00.000Z",
    stopped_at: str | None = "2024-01-01T00:00:01.000Z",
) -> dict[str, Any]:
    """Build an n8n-shaped execution payload for tests."""
    nodes = nodes if nodes is not None else {
        "Start": [
            {
                "startTime": 1704067200000,
                "executionTime": 5,
                "data": {"main": [[{"json": {"go": True}}]]},
                "source": [],
            }
        ],
        "AI Agent": [
            {
                "startTime": 1704067200010,
                "executionTime": 250,
                "data": {
                    "main": [[{"json": {"reply": "hi"}}]],
                    "n8n.nodes.langchain.agent": {
                        "model": "claude-sonnet-4-6",
                        "tokens": {"input": 12, "output": 4},
                    },
                },
                "source": [{"previousNode": "Start"}],
            }
        ],
    }
    return {
        "id": execution_id,
        "workflowId": workflow_id,
        "workflowData": {"name": workflow_name},
        "mode": mode,
        "finished": finished,
        "status": status,
        "startedAt": started_at,
        "stoppedAt": stopped_at,
        "data": {"resultData": {"runData": nodes}},
    }


def page(executions: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a list of executions in the standard n8n list response shape."""
    return {"data": executions, "nextCursor": None}
