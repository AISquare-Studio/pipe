"""n8n Source connector — streams workflow executions as TraceBatch envelopes.

Each yielded envelope carries a *full* TraceBatch payload (one `trace_id`
plus all spans for that execution) so the AISquare gateway can ingest it in
one request. The envelope ``data`` shape mirrors the in-gateway connector at
``AISquare-Explainability-SDK/gateway/connectors/n8n.py`` exactly — see
``spans.py`` for the canonical shaper.

Two-phase polling per cycle:

1. **Running executions** — :meth:`N8nClient.list_running_executions` with
   ``include_data=True``. For each in-progress run we emit:
   - a stub envelope (one pending span per workflow node) on first sight;
     idempotent so re-emissions during continued polls dedupe at the gateway.
   - a progress envelope (with the partial ``runData`` shaped into real spans)
     when the workflow has accumulated more completed nodes. The idempotency
     key folds in a content fingerprint so steady-state polls dedupe to a
     no-op and only a fresh node-completion produces a new ingest.

2. **Finished executions** — :meth:`N8nClient.list_executions` filtered to
   ``finished=true`` and ``id > cursor``. For each, emit the full final
   envelope and advance the cursor only past these — never past unfinished
   runs, which would lose their final-state emission.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_n8n.client import (
    LEGACY_CURSOR_PATH,
    N8nClient,
    default_cursor_path,
    load_cursor,
    migrate_legacy_cursor,
    save_cursor,
)
from aisquare_pipe_n8n.spans import (
    execution_to_stub_trace_batch,
    execution_to_trace_batch,
    progress_fingerprint,
)

logger = logging.getLogger("aisquare.pipe.n8n")

TRACE_CONTENT_TYPE = "application/x-aisquare-trace+json"

# Idempotency key prefixes. The aisquare-gateway sink lifts
# ``envelope.metadata["idempotency_key"]`` onto the ``X-Idempotency-Key`` HTTP
# header, and the gateway dedupes by it — so the keys must stay stable across
# polls for the same logical emission.
IDEMPOTENCY_PREFIX_STUB = "n8n:stub"
IDEMPOTENCY_PREFIX_PROGRESS = "n8n:progress"
IDEMPOTENCY_PREFIX_FINAL = "n8n:final"


class N8nSource(SourceConnector):
    """Pulls workflow execution events from an n8n instance.

    Yields one TraceBatch-shaped envelope per execution emission:

    * an in-progress stub when the run first appears,
    * a progress emission when more nodes complete inside the still-running run,
    * a final emission when ``finished`` flips to true.
    """

    name = "n8n"
    version = "0.2.1"
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
            description=(
                "File path for last-seen execution ID (default: "
                "~/.cache/aisquare-pipe/n8n-cursor.json, honouring "
                "$XDG_CACHE_HOME)"
            ),
        ),
        "workflow_id_filter": MetaField(
            type=list,
            required=False,
            description="Optional list of workflow IDs to limit to",
        ),
        "include_running": MetaField(
            type=bool,
            required=False,
            default=True,
            description="Emit stub + progress envelopes for in-progress runs (set false to disable the live-graph feed)",
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
            description="One of stub, progress, final",
        ),
        "idempotency_key": MetaField(
            type=str,
            required=True,
            description="Stable key the sink forwards as X-Idempotency-Key",
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Poll n8n indefinitely and yield TraceBatch envelopes.

        Supported PullParams keys (for tests/operations):
            max_polls (int): cap the number of poll iterations (default: unlimited)
            sleep (callable): override for time.sleep (default: time.sleep)
        """
        client = N8nClient(config)

        poll_interval = int(
            config.get(
                "poll_interval_seconds",
                self.CONFIG_SPEC["poll_interval_seconds"].default,
            )
        )
        cursor_path = config.get("cursor_path")
        if not cursor_path:
            cursor_path = default_cursor_path()
            migrate_legacy_cursor(LEGACY_CURSOR_PATH, cursor_path)
        workflow_ids = config.get("workflow_id_filter") or None
        include_running = bool(
            config.get("include_running", self.CONFIG_SPEC["include_running"].default)
        )

        max_polls: int | None = None
        sleep = time.sleep
        if params is not None:
            max_polls = params.get("max_polls", None)
            sleep = params.get("sleep", time.sleep)

        polls = 0
        while max_polls is None or polls < max_polls:
            cursor = load_cursor(cursor_path)

            if include_running:
                yield from self._yield_running(client, workflow_ids, cursor)

            new_cursor = cursor
            for envelope, exec_id in self._yield_finished(
                client, workflow_ids, cursor
            ):
                yield envelope
                if exec_id > new_cursor:
                    new_cursor = exec_id

            if new_cursor > cursor:
                save_cursor(cursor_path, new_cursor)

            polls += 1
            if max_polls is None or polls < max_polls:
                sleep(poll_interval)

    def _yield_running(
        self,
        client: N8nClient,
        workflow_ids: list[str] | None,
        cursor: int,
    ) -> Iterator[DataEnvelope]:
        """Emit stub + progress envelopes for each running execution that's
        newer than the cursor. Stale-cache trick: workflow-definition lookups
        are deduplicated per poll cycle so several concurrent executions of the
        same workflow share one /workflows/{id} call."""
        try:
            running = client.list_running_executions(
                workflow_ids=workflow_ids, include_data=True
            )
        except Exception:
            logger.exception("n8n: list_running_executions failed")
            return

        wf_def_cache: dict[str, dict[str, Any] | None] = {}

        def _wf_def(workflow_id_str: str) -> dict[str, Any] | None:
            if workflow_id_str in wf_def_cache:
                return wf_def_cache[workflow_id_str]
            defn = client.get_workflow_definition(workflow_id_str)
            wf_def_cache[workflow_id_str] = defn
            return defn

        for execution in running:
            exec_id_raw = execution.get("id")
            try:
                exec_id = int(exec_id_raw) if exec_id_raw is not None else 0
            except (TypeError, ValueError):
                exec_id = 0
            if exec_id == 0 or (cursor and exec_id <= cursor):
                continue

            workflow_id = str(execution.get("workflowId") or "")
            workflow_def = _wf_def(workflow_id) if workflow_id else None

            try:
                trace_id, stub_batch = execution_to_stub_trace_batch(
                    execution, workflow_def=workflow_def
                )
            except Exception:
                logger.exception(
                    "n8n: stub shaping failed for execution %s", exec_id
                )
                continue

            if stub_batch.get("spans"):
                yield self._envelope(
                    stub_batch,
                    trace_id=trace_id,
                    execution=execution,
                    event="stub",
                    idempotency_key=f"{IDEMPOTENCY_PREFIX_STUB}:{trace_id}",
                )

            # Only emit a progress envelope when n8n has accumulated per-node
            # runData beyond the bare run shell.
            run_data = (
                (execution.get("data") or {}).get("resultData") or {}
            ).get("runData") or {}
            if not run_data:
                continue

            try:
                _, progress_batch = execution_to_trace_batch(execution)
            except Exception:
                logger.exception(
                    "n8n: progress shaping failed for execution %s", exec_id
                )
                continue

            if progress_batch.get("spans"):
                fingerprint = progress_fingerprint(progress_batch)
                yield self._envelope(
                    progress_batch,
                    trace_id=trace_id,
                    execution=execution,
                    event="progress",
                    idempotency_key=(
                        f"{IDEMPOTENCY_PREFIX_PROGRESS}:{trace_id}:{fingerprint}"
                    ),
                )

    def _yield_finished(
        self,
        client: N8nClient,
        workflow_ids: list[str] | None,
        cursor: int,
    ) -> Iterator[tuple[DataEnvelope, int]]:
        """Emit final envelopes for finished executions newer than the cursor.
        Yields (envelope, exec_id) so the caller can advance the cursor only
        past successfully-shaped emissions."""
        executions = client.list_executions(
            last_id=cursor,
            workflow_ids=workflow_ids,
            include_data=True,
            finished_only=True,
        )
        for execution in executions:
            exec_id_raw = execution.get("id")
            try:
                exec_id = int(exec_id_raw) if exec_id_raw is not None else 0
            except (TypeError, ValueError):
                exec_id = 0
            if exec_id == 0:
                continue

            try:
                trace_id, trace_batch = execution_to_trace_batch(execution)
            except Exception:
                logger.exception(
                    "n8n: final shaping failed for execution %s", exec_id
                )
                continue
            if not trace_batch.get("spans"):
                continue

            envelope = self._envelope(
                trace_batch,
                trace_id=trace_id,
                execution=execution,
                event="final",
                idempotency_key=f"{IDEMPOTENCY_PREFIX_FINAL}:{trace_id}",
            )
            yield envelope, exec_id

    def _envelope(
        self,
        trace_batch: dict[str, Any],
        *,
        trace_id: str,
        execution: dict[str, Any],
        event: str,
        idempotency_key: str,
    ) -> DataEnvelope:
        workflow_data = execution.get("workflowData") or {}
        return DataEnvelope(
            content_type=TRACE_CONTENT_TYPE,
            data=trace_batch,
            source_id=self.name,
            metadata={
                "n8n_execution_id": str(execution.get("id", "")),
                "n8n_workflow_id": str(execution.get("workflowId", "")),
                "n8n_workflow_name": workflow_data.get("name", ""),
                "n8n_event": event,
                "trace_id": trace_id,
                "idempotency_key": idempotency_key,
            },
        )

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
