"""n8n Source connector — streams workflow executions into the pipeline."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_n8n.client import N8nClient, load_cursor, save_cursor

logger = logging.getLogger("aisquare.pipe.n8n")

TRACE_CONTENT_TYPE = "application/x-aisquare-trace+json"

EVENT_WORKFLOW_START = "workflow_start"
EVENT_NODE_STEP = "node_step"
EVENT_WORKFLOW_COMPLETE = "workflow_complete"


class N8nSource(SourceConnector):
    """Pulls workflow execution events from an n8n instance.

    The connector polls the n8n Executions API, yielding three kinds of
    envelopes per execution:

    * one ``workflow_start`` envelope keyed by ``execution.id``
    * one ``node_step`` envelope per executed node in ``runData``
    * one ``workflow_complete`` envelope when ``finished`` is true

    All envelopes share the ``application/x-aisquare-trace+json`` content
    type — the canonical shape the AISquare gateway consumes.
    """

    name = "n8n"
    version = "0.1.0"
    output_types = [TRACE_CONTENT_TYPE]
    auth_type = AuthType.API_KEY

    description = "Pulls workflow execution events from an n8n instance."
    docs_url = "https://docs.n8n.io/api/"

    CONFIG_SPEC: dict[str, MetaField] = {
        "n8n_url": MetaField(
            type=str,
            required=True,
            description="Base URL of the n8n instance (e.g. http://n8n:5678)",
        ),
        "api_key": MetaField(
            type=str,
            required=True,
            description="n8n API key",
        ),
        "poll_interval_seconds": MetaField(
            type=int,
            required=False,
            default=5,
            description="How often to poll for new executions",
        ),
        "cursor_path": MetaField(
            type=str,
            required=False,
            default="/tmp/n8n-pipe-cursor.json",
            description="File path for last-seen execution ID",
        ),
        "workflow_id_filter": MetaField(
            type=list,
            required=False,
            description="Optional list of workflow IDs to limit to",
        ),
    }

    metadata_spec = {
        "n8n_execution_id": MetaField(
            type=str, required=True, description="n8n execution ID"
        ),
        "n8n_workflow_id": MetaField(
            type=str, required=True, description="n8n workflow ID"
        ),
        "n8n_workflow_name": MetaField(
            type=str, required=False, description="Human-readable workflow name"
        ),
        "n8n_event": MetaField(
            type=str,
            required=True,
            description="One of workflow_start, node_step, workflow_complete",
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Poll n8n indefinitely and yield envelopes for new executions.

        Supported PullParams keys (intended for tests/operations):
            max_polls (int): cap the number of poll iterations (default: unlimited)
            sleep (callable): override for time.sleep (default: time.sleep)
        """
        client = N8nClient(config)

        poll_interval = int(
            config.get("poll_interval_seconds", self.CONFIG_SPEC["poll_interval_seconds"].default)
        )
        cursor_path = config.get(
            "cursor_path", self.CONFIG_SPEC["cursor_path"].default
        )
        workflow_ids = config.get("workflow_id_filter") or None

        max_polls: int | None = None
        sleep = time.sleep
        if params is not None:
            max_polls = params.get("max_polls", None)
            sleep = params.get("sleep", time.sleep)

        polls = 0
        while max_polls is None or polls < max_polls:
            cursor = load_cursor(cursor_path)
            executions = client.list_executions(
                last_id=cursor, workflow_ids=workflow_ids, include_data=True
            )

            new_cursor = cursor
            for execution in executions:
                exec_id = int(execution.get("id", 0))
                yield from _envelopes_for_execution(execution, self.name)
                if exec_id > new_cursor:
                    new_cursor = exec_id

            if new_cursor > cursor:
                save_cursor(cursor_path, new_cursor)

            polls += 1
            if max_polls is None or polls < max_polls:
                sleep(poll_interval)

    def validate_config(self, config: dict) -> bool:
        if not config.get("n8n_url") or not config.get("api_key"):
            return False
        try:
            return N8nClient(config).validate()
        except Exception as e:
            logger.warning("n8n validate_config failed: %s", e)
            return False

    def supports_streaming(self) -> bool:
        return False


def _envelopes_for_execution(
    execution: dict[str, Any], source_id: str
) -> Iterator[DataEnvelope]:
    """Expand a single n8n execution into ordered trace envelopes."""
    exec_id = str(execution.get("id", ""))
    workflow_id = str(execution.get("workflowId", ""))
    workflow_data = execution.get("workflowData") or {}
    workflow_name = workflow_data.get("name", "")

    base_meta = {
        "n8n_execution_id": exec_id,
        "n8n_workflow_id": workflow_id,
        "n8n_workflow_name": workflow_name,
    }

    # 1. workflow_start
    yield DataEnvelope(
        content_type=TRACE_CONTENT_TYPE,
        data={
            "event": EVENT_WORKFLOW_START,
            "execution_id": exec_id,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "mode": execution.get("mode"),
            "started_at": execution.get("startedAt"),
        },
        source_id=source_id,
        metadata={**base_meta, "n8n_event": EVENT_WORKFLOW_START},
    )

    # 2. node_step per executed node
    run_data = (
        execution.get("data", {})
        .get("resultData", {})
        .get("runData", {})
    ) or {}

    for node_name, runs in run_data.items():
        if not isinstance(runs, list):
            continue
        for run_index, run in enumerate(runs):
            yield DataEnvelope(
                content_type=TRACE_CONTENT_TYPE,
                data=_build_node_step(
                    node_name, run_index, run, exec_id, workflow_id
                ),
                source_id=source_id,
                metadata={
                    **base_meta,
                    "n8n_event": EVENT_NODE_STEP,
                    "n8n_node_name": node_name,
                    "n8n_run_index": run_index,
                },
            )

    # 3. workflow_complete (only when n8n marks the run finished)
    if execution.get("finished"):
        yield DataEnvelope(
            content_type=TRACE_CONTENT_TYPE,
            data={
                "event": EVENT_WORKFLOW_COMPLETE,
                "execution_id": exec_id,
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "status": execution.get("status"),
                "stopped_at": execution.get("stoppedAt"),
                "finished": True,
            },
            source_id=source_id,
            metadata={**base_meta, "n8n_event": EVENT_WORKFLOW_COMPLETE},
        )


def _build_node_step(
    node_name: str,
    run_index: int,
    run: dict[str, Any],
    exec_id: str,
    workflow_id: str,
) -> dict[str, Any]:
    """Assemble the node_step payload, surfacing LangChain AI details when present."""
    step: dict[str, Any] = {
        "event": EVENT_NODE_STEP,
        "execution_id": exec_id,
        "workflow_id": workflow_id,
        "node_name": node_name,
        "run_index": run_index,
        "started_at": run.get("startTime"),
        "finished_at": _finished_at(run),
        "execution_time_ms": run.get("executionTime"),
        "error": run.get("error"),
        "source": run.get("source"),
    }

    inputs = run.get("inputData") or run.get("data", {}).get("main")
    if inputs is not None:
        step["input_items"] = inputs

    outputs = run.get("data", {}).get("main") if isinstance(run.get("data"), dict) else None
    if outputs is not None:
        step["output_items"] = outputs

    ai = _extract_langchain_details(run)
    if ai:
        step["ai"] = ai

    return step


def _finished_at(run: dict[str, Any]) -> Any:
    """Compute a finished_at timestamp from startTime + executionTime if absent."""
    if "finishedAt" in run:
        return run["finishedAt"]
    start = run.get("startTime")
    duration = run.get("executionTime")
    if isinstance(start, (int, float)) and isinstance(duration, (int, float)):
        return start + duration
    return None


def _extract_langchain_details(run: dict[str, Any]) -> dict[str, Any]:
    """Surface anything under data.* that looks like a LangChain AI node payload."""
    details: dict[str, Any] = {}
    data = run.get("data")
    if not isinstance(data, dict):
        return details
    for key, value in data.items():
        if isinstance(key, str) and key.startswith("n8n.nodes.langchain."):
            details[key] = value
    return details
