"""n8n execution → TraceBatch span shaping.

Mirrors the in-gateway connector at
``AISquare-Explainability-SDK/gateway/connectors/n8n.py`` so a trace produced
by the standalone pipe-n8n container is byte-equivalent to one produced by
the legacy in-gateway poller. Any divergence here breaks the parity the
gateway's structural worker, FE rendering, and policy detector depend on.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ─── ID + value coercion helpers ─────────────────────────────────────────


def safe_id(raw: str, length: int = 128) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw)
    return safe[:length]


def attr_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_nano(value: Any, fallback: Optional[int] = None) -> Optional[int]:
    """Coerce timestamps to epoch nanoseconds. n8n returns execution-level
    fields as ISO strings; node-level startTime is epoch-millis. The gateway's
    structural worker expects integer nanos."""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        if value > 1e15:
            return int(value)
        return int(value * 1_000_000)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            return fallback
    return fallback


def iso_for_attr(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _end_time_for_node_ns(run: Dict[str, Any], fallback_ns: int) -> int:
    start_ns = to_nano(run.get("startTime"), fallback_ns) or fallback_ns
    exec_ms = run.get("executionTime")
    if isinstance(exec_ms, (int, float)):
        return start_ns + int(exec_ms * 1_000_000)
    return start_ns


# ─── Node classification + workflow domain ───────────────────────────────


_LLM_KEYWORDS = (
    "openai", "anthropic", "claude", "gpt", "chat model", "language model",
    "llm", "langchain", "chain", "agent", "embeddings",
)
_TOOL_KEYWORDS = (
    "http request", "webhook", "drive", "sheets", "gmail", "slack",
    "notion", "airtable", "postgres", "mysql", "mongodb", "supabase",
    "telegram", "github", "linear", "asana", "stripe", "sendgrid",
)


def classify_node_kind(node_name: str) -> str:
    """OpenInference span kind for FE icon rendering."""
    lower = node_name.lower()
    for kw in _LLM_KEYWORDS:
        if kw in lower:
            return "LLM"
    for kw in _TOOL_KEYWORDS:
        if kw in lower:
            return "TOOL"
    return "CHAIN"


def domain_from_workflow(workflow_data: Dict[str, Any], workflow_name: str) -> str:
    """Pick a domain tag for the FE's rule-routing. `domain:<value>` tags on
    the n8n workflow win; otherwise heuristic from workflow name; otherwise
    `n8n_workflow`."""
    tags = workflow_data.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            tag = t.get("name") if isinstance(t, dict) else t
            if isinstance(tag, str) and tag.lower().startswith("domain:"):
                return tag.split(":", 1)[1].strip().lower() or "n8n_workflow"
    name_lc = (workflow_name or "").lower()
    if any(kw in name_lc for kw in ("eim", "hrms", "talent", "cv ", "resume", "matching")):
        return "hrms"
    if any(kw in name_lc for kw in ("claim", "insurance")):
        return "insurance_claims"
    if any(kw in name_lc for kw in ("risk", "compliance", "supplier")):
        return "supply_chain_risk"
    return "n8n_workflow"


# ─── LLM model lookup ────────────────────────────────────────────────────


def build_node_model_lookup(workflow_data: Dict[str, Any]) -> Dict[str, str]:
    """Walk `workflowData.nodes[]` and build `node_name → model_id` lookup.

    n8n LLM sub-nodes store the chosen model in `parameters.model`. Newer
    versions wrap it in `{__rl: true, value: "claude-...", ...}`; older
    versions pass the bare string. Handle both shapes.
    """
    out: Dict[str, str] = {}
    nodes = (workflow_data or {}).get("nodes") or []
    if not isinstance(nodes, list):
        return out
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_name = node.get("name")
        if not isinstance(node_name, str) or not node_name:
            continue
        params = node.get("parameters") or {}
        if not isinstance(params, dict):
            continue
        model = params.get("model")
        if isinstance(model, dict):
            mid = model.get("value") or model.get("cachedResultName")
            if isinstance(mid, str) and mid:
                out[node_name] = mid
        elif isinstance(model, str) and model:
            out[node_name] = model
    return out


def model_name_from_n8n(
    node_name: str,
    run: Dict[str, Any],
    node_model_lookup: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Pull the actual LLM model id for an n8n LLM node."""
    if node_model_lookup:
        mid = node_model_lookup.get(node_name)
        if mid:
            return mid

    data = run.get("data") or {}
    if isinstance(data, dict):
        for port_key, port_value in data.items():
            if "languageModel" not in port_key.lower() and "main" not in port_key.lower():
                continue
            if not isinstance(port_value, list) or not port_value:
                continue
            first_port = port_value[0] if isinstance(port_value[0], list) else None
            if not first_port:
                continue
            item = first_port[0] if isinstance(first_port[0], dict) else None
            if not item:
                continue
            j = item.get("json") if isinstance(item.get("json"), dict) else item
            if not isinstance(j, dict):
                continue
            try:
                gens = (((j.get("response") or {}).get("generations") or [])[0] or [])
                if gens:
                    ginfo = gens[0].get("generationInfo") or {}
                    m = ginfo.get("model_name") or ginfo.get("model") or ginfo.get("modelId")
                    if isinstance(m, str) and m:
                        return m
            except Exception:
                pass
            for k in ("model", "modelName", "model_id"):
                m = j.get(k)
                if isinstance(m, str) and m:
                    return m

    name_lc = (node_name or "").lower()
    if "anthropic" in name_lc or "claude" in name_lc:
        return "Anthropic Claude (model unspecified)"
    if "openai" in name_lc or "gpt" in name_lc:
        return "OpenAI GPT (model unspecified)"
    return None


# ─── Per-node attribute builder ──────────────────────────────────────────


_PREVIEW_MAX = 16000  # OTel attribute size guard


def node_attrs(
    node_name: str,
    run_index: int,
    run: Dict[str, Any],
    node_model_lookup: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    kind = classify_node_kind(node_name)
    out: Dict[str, Any] = {
        "n8n.node_name": node_name,
        "n8n.run_index": run_index,
        "n8n.execution_time_ms": run.get("executionTime"),
        "openinference.span.kind": kind,
    }
    if kind == "TOOL":
        out["tool.name"] = node_name
    elif kind == "LLM":
        out["llm.system"] = node_name
        model_name = model_name_from_n8n(node_name, run, node_model_lookup)
        if model_name:
            out["llm.model_name"] = model_name

    data = run.get("data") or {}
    captured_preview = False
    if isinstance(data, dict):
        for port_key, port_value in data.items():
            if not isinstance(port_value, list) or not port_value:
                continue
            first_port = port_value[0] if isinstance(port_value[0], list) else None
            if first_port is None or not first_port:
                continue
            attr_prefix = "n8n.output" if port_key == "main" else f"n8n.{port_key}"
            out[f"{attr_prefix}_items"] = len(first_port)
            if not captured_preview:
                first_item = first_port[0]
                preview = first_item.get("json") if isinstance(first_item, dict) else first_item
                try:
                    preview_str = json.dumps(preview, default=str, indent=2)[:_PREVIEW_MAX]
                    out["n8n.output_preview"] = preview_str
                except (TypeError, ValueError):
                    preview_str = str(preview)[:_PREVIEW_MAX]
                    out["n8n.output_preview"] = preview_str
                captured_preview = True
                if kind == "TOOL":
                    out["tool.return_value"] = preview_str
                elif kind == "LLM":
                    out["llm.output_messages.0.message.content"] = preview_str
                if isinstance(first_item, dict) and isinstance(first_item.get("binary"), dict):
                    out["n8n.binary_keys"] = list(first_item["binary"].keys())[:10]

    source = run.get("source")
    if isinstance(source, list):
        out["n8n.input_items"] = len(source)
    err = run.get("error")
    if err:
        out["n8n.error"] = err if isinstance(err, str) else (err.get("message") or json.dumps(err, default=str))
    return out


# ─── Full TraceBatch builders ────────────────────────────────────────────


def execution_to_trace_batch(execution: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Return (trace_id, trace_batch_dict). One execution → start + per-node + complete."""
    execution_id = str(execution.get("id", ""))
    workflow_id = str(execution.get("workflowId", "")) or "unknown"
    workflow_name = (
        (execution.get("workflowData") or {}).get("name")
        or execution.get("workflowName")
        or workflow_id
    )
    trace_id = safe_id(f"n8n-{workflow_id}-{execution_id}")

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
    started_at_ns = to_nano(execution.get("startedAt"), now_ns) or now_ns
    stopped_at_ns = to_nano(execution.get("stoppedAt") or execution.get("finishedAt"))
    started_at_iso = iso_for_attr(execution.get("startedAt")) or now_iso()
    stopped_at_iso = iso_for_attr(execution.get("stoppedAt") or execution.get("finishedAt"))
    mode = execution.get("mode") or "trigger"
    finished = bool(execution.get("finished"))
    status_str = (
        "success" if finished and execution.get("status") != "error"
        else (execution.get("status") or "running")
    )

    start_span_id = f"{trace_id}-start"
    spans: List[Dict[str, Any]] = []

    base_attrs: Dict[str, Any] = {
        "n8n.event": "workflow_start",
        "n8n.execution_id": execution_id,
        "n8n.workflow_id": workflow_id,
        "n8n.workflow_name": workflow_name,
        "n8n.mode": mode,
        "n8n.started_at": started_at_iso,
        "agent.name": workflow_name,
        "agent.metadata.source": "n8n",
        "agent.metadata.workflow_name": workflow_name,
        "agent.metadata.agent_name": workflow_name,
        "openinference.span.kind": "AGENT",
        "agent.metadata.run.title": f"{workflow_name} · execution #{execution_id}",
        "agent.metadata.run_kind": "n8n_execution",
        "agent.metadata.session.id": f"n8n-workflow-{workflow_id}",
        "agent.metadata.parent_run_id": f"n8n-workflow-{workflow_id}",
        "agent.metadata.domain": domain_from_workflow(
            execution.get("workflowData") or {}, workflow_name
        ),
    }
    root_end_time = stopped_at_ns if finished else None
    spans.append(
        {
            "span_id": start_span_id,
            "trace_id": trace_id,
            "parent_span_id": None,
            "name": workflow_name,
            "kind": "INTERNAL",
            "start_time": started_at_ns,
            "end_time": root_end_time,
            "attributes": {k: attr_safe(v) for k, v in base_attrs.items()},
            "status": {"code": "OK"},
        }
    )

    node_model_lookup = build_node_model_lookup(execution.get("workflowData") or {})
    run_data = ((execution.get("data") or {}).get("resultData") or {}).get("runData") or {}
    for node_name, runs in run_data.items():
        if not isinstance(runs, list):
            continue
        for run_index, run in enumerate(runs):
            attrs = node_attrs(node_name, run_index, run, node_model_lookup)
            attrs.update(
                {
                    "n8n.event": "node_step",
                    "n8n.execution_id": execution_id,
                    "n8n.workflow_id": workflow_id,
                    "n8n.workflow_name": workflow_name,
                }
            )
            node_start_ns = to_nano(run.get("startTime"), started_at_ns) or started_at_ns
            spans.append(
                {
                    "span_id": safe_id(f"{trace_id}-node-{node_name}-{run_index}"),
                    "trace_id": trace_id,
                    "parent_span_id": start_span_id,
                    "name": node_name,
                    "kind": "INTERNAL",
                    "start_time": node_start_ns,
                    "end_time": _end_time_for_node_ns(run, node_start_ns),
                    "attributes": {k: attr_safe(v) for k, v in attrs.items()},
                    "status": {"code": "OK" if run.get("error") is None else "ERROR"},
                }
            )

    if finished:
        complete_attrs = {
            "n8n.event": "workflow_complete",
            "n8n.execution_id": execution_id,
            "n8n.workflow_id": workflow_id,
            "n8n.workflow_name": workflow_name,
            "n8n.status": status_str,
            "n8n.stopped_at": stopped_at_iso or now_iso(),
            "openinference.span.kind": "CHAIN",
        }
        spans.append(
            {
                "span_id": f"{trace_id}-complete",
                "trace_id": trace_id,
                "parent_span_id": start_span_id,
                "name": f"{workflow_name} · complete",
                "kind": "INTERNAL",
                "start_time": stopped_at_ns or started_at_ns,
                "end_time": stopped_at_ns or now_ns,
                "attributes": {k: attr_safe(v) for k, v in complete_attrs.items()},
                "status": {"code": "OK" if status_str == "success" else "ERROR"},
            }
        )

    return trace_id, {"trace_id": trace_id, "spans": spans}


def execution_to_stub_trace_batch(
    execution: Dict[str, Any],
    *,
    workflow_def: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Stub trace for an in-progress execution. When ``workflow_def`` is
    supplied, emits one pending span per node so the FE shows the full graph
    shape on first poll; the final batch overrides those spans by span_id
    when the execution finishes."""
    execution_id = str(execution.get("id", ""))
    workflow_id = str(execution.get("workflowId", "")) or "unknown"
    workflow_name = (
        (workflow_def or {}).get("name")
        or (execution.get("workflowData") or {}).get("name")
        or execution.get("workflowName")
        or workflow_id
    )
    trace_id = safe_id(f"n8n-{workflow_id}-{execution_id}")
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
    started_at_ns = to_nano(execution.get("startedAt"), now_ns) or now_ns
    started_at_iso = iso_for_attr(execution.get("startedAt")) or now_iso()
    mode = execution.get("mode") or "trigger"

    start_span_id = f"{trace_id}-start"
    spans: List[Dict[str, Any]] = [
        {
            "span_id": start_span_id,
            "trace_id": trace_id,
            "parent_span_id": None,
            "name": workflow_name,
            "kind": "INTERNAL",
            "start_time": started_at_ns,
            "end_time": None,
            "attributes": {
                "n8n.event": "workflow_start",
                "n8n.status": "running",
                "n8n.execution_id": execution_id,
                "n8n.workflow_id": workflow_id,
                "n8n.workflow_name": workflow_name,
                "n8n.mode": mode,
                "n8n.started_at": started_at_iso,
                "agent.name": workflow_name,
                "agent.metadata.source": "n8n",
                "agent.metadata.workflow_name": workflow_name,
                "agent.metadata.agent_name": workflow_name,
                "openinference.span.kind": "AGENT",
                "agent.metadata.run.title": f"{workflow_name} · execution #{execution_id}",
                "agent.metadata.run_kind": "n8n_execution",
                "agent.metadata.session.id": f"n8n-workflow-{workflow_id}",
                "agent.metadata.parent_run_id": f"n8n-workflow-{workflow_id}",
                "agent.metadata.domain": domain_from_workflow(
                    (execution.get("workflowData") or workflow_def or {}),
                    workflow_name,
                ),
            },
            "status": {"code": "OK"},
        }
    ]

    if workflow_def:
        for node in workflow_def.get("nodes") or []:
            node_name = node.get("name")
            if not isinstance(node_name, str) or not node_name:
                continue
            spans.append(
                {
                    "span_id": safe_id(f"{trace_id}-node-{node_name}-0"),
                    "trace_id": trace_id,
                    "parent_span_id": start_span_id,
                    "name": f"n8n.node:{node_name}",
                    "kind": "INTERNAL",
                    "start_time": started_at_ns,
                    "end_time": started_at_ns,
                    "attributes": {
                        "n8n.event": "node_step",
                        "n8n.status": "pending",
                        "n8n.node_name": node_name,
                        "n8n.node_type": node.get("type") or "",
                        "n8n.run_index": 0,
                        "n8n.execution_id": execution_id,
                        "n8n.workflow_id": workflow_id,
                        "n8n.workflow_name": workflow_name,
                        "openinference.span.kind": classify_node_kind(node_name),
                    },
                    "status": {"code": "OK"},
                }
            )

    return trace_id, {"trace_id": trace_id, "spans": spans}


def progress_fingerprint(trace_batch: Dict[str, Any]) -> str:
    """Short stable hash of the set of completed-node span_ids in this batch.
    Used as part of the idempotency key for in-progress emissions so the same
    state polls dedupe; only a new node-completion flips the fingerprint."""
    spans = trace_batch.get("spans") or []
    node_ids = sorted(
        s.get("span_id", "")
        for s in spans
        if (s.get("attributes") or {}).get("n8n.event") == "node_step"
    )
    h = hashlib.sha256("|".join(node_ids).encode("utf-8")).hexdigest()
    return h[:12]
