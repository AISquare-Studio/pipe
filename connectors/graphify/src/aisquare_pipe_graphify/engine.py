"""GraphifyEngine — one implementation behind both connector faces.

Two-tier build (verified against graphifyy 0.8.36):

* **enriched** — ``graphify extract . --no-viz --backend <b> --model <m>``
  followed by ``graphify cluster-only .`` with the backend's key in a
  scrubbed env. The two-step shape is load-bearing: ``extract`` writes ONLY
  ``graph.json`` — ``GRAPH_REPORT.md`` (and named communities) come from
  ``cluster-only``. NEVER runs without an explicit ``--backend``: a
  backend-less ``extract`` does NOT degrade to a free AST pass — it
  auto-detects a backend from ambient env vars and crashes when that
  backend's package/key is missing.
* **ast** — ``graphify update .`` — the version-correct keyless path
  ("re-extract code files and update the graph (no LLM needed)"). Writes
  both artifacts itself. Full structural graph (nodes/edges/communities),
  no LLM-inferred edges, no community naming.

Enriched failures fall back to a *working* AST graph in the same run (the
failure tail rides along as ``enrichment_error``) instead of erroring the
whole build — a dead key should degrade the product, not kill it. The same
degrade applies when an "enriched" run leaves no usable report behind
(belt-and-braces against engine version drift; an enriched pass that paid
the LLM but produced nothing storable must never fail the build outright).

Cost containment, in build order: a **doc-volume preflight** counts the
doc-class bytes (the ONLY content the LLM ever sees — code is always local
AST) and degrades to the AST tier above a configurable ceiling; the
enriched pass runs on a small **model** by default (extraction is
pattern-matching, not reasoning); and per-run **LLM token/cost telemetry**
is parsed from the extract output so spend is observable per build.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess  # nosec B404 — fixed argv, shell=False throughout
import tarfile
from dataclasses import dataclass

import requests

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_graphify.constants import (
    DEFAULT_CLUSTER_TIMEOUT,
    DEFAULT_DOC_VOLUME_CAP_BYTES,
    DEFAULT_EXTRACT_FLAGS,
    DEFAULT_EXTRACT_MODEL,
    DEFAULT_EXTRACT_TIMEOUT,
    DEFAULT_UPDATE_TIMEOUT,
    DOC_EXTENSIONS,
    KEY_ENV,
)

logger = logging.getLogger("aisquare.pipe.graphify")

_OUT_DIRNAME = "graphify-out"
_REPORT_NAME = "GRAPH_REPORT.md"
_GRAPH_JSON_NAME = "graph.json"
_ANALYSIS_NAME = ".graphify_analysis.json"
_VERSION_TIMEOUT = 15
_PREFLIGHT_TIMEOUT = 15

# Dirs the doc-volume preflight never descends into. Deliberately short:
# vendored doc trees SHOULD trip the cap — that's the blow-up case it exists
# to catch.
_DOC_SCAN_SKIP_DIRS = {".git", _OUT_DIRNAME}

# graphify extract's per-run spend line, e.g.
# "[graphify extract] tokens: 1,234 in / 567 out, est. cost: $0.0123".
# Lenient — wording drift leaves the fields 0; .graphify_analysis.json is the
# fallback source. (cost.json is agent-skill-only and never written headless.)
_LLM_TOKENS_RE = re.compile(r"tokens:\s*([\d,]+)\s*in\s*/\s*([\d,]+)\s*out", re.IGNORECASE)
_LLM_COST_RE = re.compile(r"est\.?\s*cost:\s*\$([0-9][0-9,]*\.?[0-9]*)", re.IGNORECASE)

# Lenient stat parsers (ported from the backend) — wording varies across
# graphify versions; counts are a nicety, a miss leaves the field 0. The
# "<N> nodes" alternative allows only same-line whitespace so a
# "Nodes: 1\nEdges: 2" header can't cross-match.
_NODES_RE = re.compile(r"([\d,]+)[ \t]+nodes\b|nodes?\s*(?:count)?\s*[:=]\s*([\d,]+)", re.IGNORECASE)
_EDGES_RE = re.compile(r"([\d,]+)[ \t]+edges\b|edges?\s*(?:count)?\s*[:=]\s*([\d,]+)", re.IGNORECASE)
_COMMUNITIES_RE = re.compile(
    r"([\d,]+)[ \t]+communit(?:y|ies)\b|communit(?:y|ies)\s*(?:count)?\s*[:=]\s*([\d,]+)",
    re.IGNORECASE,
)


def _first_int(pattern: re.Pattern, text: str) -> int:
    match = pattern.search(text or "")
    if not match:
        return 0
    for group in match.groups():
        if group:
            try:
                return int(group.replace(",", ""))
            except (TypeError, ValueError):
                return 0
    return 0


def parse_graph_stats(report_text: str, graph_json_text: str) -> tuple[int, int, int]:
    """Best-effort (nodes, edges, communities); never raises."""
    nodes = _first_int(_NODES_RE, report_text)
    edges = _first_int(_EDGES_RE, report_text)
    communities = _first_int(_COMMUNITIES_RE, report_text)
    if (not nodes or not edges) and graph_json_text:
        try:
            data = json.loads(graph_json_text)
            if isinstance(data, dict):
                nodes = nodes or len(data.get("nodes") or [])
                edges = edges or len(data.get("links") or data.get("edges") or [])
        except (ValueError, TypeError):
            pass
    return nodes, edges, communities


def count_tokens(text: str) -> int:
    """o200k_base token count of the report; 0 if tiktoken misbehaves."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception as exc:  # noqa: BLE001 — token count is part of the contract but never fatal
        logger.warning("graphify: token count failed: %s", exc)
        return 0


@dataclass
class GraphArtifacts:
    report_md: str
    graph_json: str
    nodes: int
    edges: int
    communities: int
    token_count: int
    tier: str  # "ast" | "enriched"
    enrichment_error: str | None
    graphify_version: str
    # Per-build LLM spend (enriched tier only; 0 on AST builds). Parsed from
    # extract stdout with .graphify_analysis.json as fallback — best-effort,
    # a parse miss leaves them 0 rather than failing the build.
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_cost_estimate: float = 0.0
    # Incremental-build state (V2 Phase 3): a tar of graphify-out's
    # manifest/graph/content-hash caches. The caller persists it and feeds it
    # into the NEXT build's ``prior_state`` — restoring it before the engine
    # runs is what makes graphify's diff-scoped/incremental paths reachable at
    # all (a fresh clone otherwise has no prior state), turning per-merge
    # enriched spend into O(changed docs). b"" when capture found nothing.
    state_tar: bytes = b""


class GraphifyEngine:
    def __init__(
        self,
        backend: str | None = None,
        api_key: str | None = None,
        *,
        model: str | None = None,
        extract_flags: str = DEFAULT_EXTRACT_FLAGS,
        extract_timeout: int = DEFAULT_EXTRACT_TIMEOUT,
        update_timeout: int = DEFAULT_UPDATE_TIMEOUT,
        cluster_timeout: int = DEFAULT_CLUSTER_TIMEOUT,
        doc_volume_cap_bytes: int = DEFAULT_DOC_VOLUME_CAP_BYTES,
        graphify_bin: str = "graphify",
        preflight: bool = True,
        fallback_to_ast: bool = True,
    ) -> None:
        self.backend = (backend or "").strip().lower() or None
        self.api_key = api_key or ""
        if self.backend and self.backend in KEY_ENV and not self.api_key:
            raise ConfigValidationError(
                f"graphify backend {self.backend!r} requires an api_key "
                f"(env var {KEY_ENV[self.backend]}); omit backend for the free AST tier"
            )
        # The cheap-model default applies ONLY to claude — handing a claude
        # model id to another backend would break its extract outright. None
        # (unset) resolves per-backend; an explicit string always wins.
        if model is None:
            model = DEFAULT_EXTRACT_MODEL if self.backend == "claude" else ""
        self.model = (model or "").strip()
        self.extract_flags = extract_flags or ""
        self.extract_timeout = int(extract_timeout)
        self.update_timeout = int(update_timeout)
        self.cluster_timeout = int(cluster_timeout)
        self.doc_volume_cap_bytes = int(doc_volume_cap_bytes or 0)
        self.graphify_bin = graphify_bin or "graphify"
        self.preflight = bool(preflight)
        self.fallback_to_ast = bool(fallback_to_ast)

    @classmethod
    def from_config(cls, config: dict) -> "GraphifyEngine":
        config = config or {}
        return cls(
            backend=config.get("backend"),
            api_key=config.get("api_key"),
            model=config.get("model"),
            extract_flags=config.get("extract_flags", DEFAULT_EXTRACT_FLAGS),
            extract_timeout=config.get("extract_timeout_seconds", DEFAULT_EXTRACT_TIMEOUT),
            update_timeout=config.get("update_timeout_seconds", DEFAULT_UPDATE_TIMEOUT),
            cluster_timeout=config.get("cluster_timeout_seconds", DEFAULT_CLUSTER_TIMEOUT),
            doc_volume_cap_bytes=config.get("doc_volume_cap_bytes", DEFAULT_DOC_VOLUME_CAP_BYTES),
            graphify_bin=config.get("graphify_bin", "graphify"),
            preflight=config.get("preflight", True),
            fallback_to_ast=config.get("fallback_to_ast", True),
        )

    # ----------------------------------------------------------------- build
    def build(self, checkout_path: str, prior_state: bytes | None = None) -> GraphArtifacts:
        if prior_state:
            self._restore_state(checkout_path, prior_state)
        tier, err, extract_stdout = "ast", None, ""
        if self._enrichment_armed():
            doc_bytes = self._doc_volume(checkout_path) if self.doc_volume_cap_bytes else 0
            if self.doc_volume_cap_bytes and doc_bytes > self.doc_volume_cap_bytes:
                # The LLM pass only ever reads doc-class content, so doc bytes
                # ARE the spend; above the ceiling this build takes the free
                # tier instead of an unbounded LLM run. Degrade, never raise:
                # an oversized doc corpus is a property of the repo, not an
                # error in the build.
                err = (
                    f"doc volume {doc_bytes} bytes exceeds cap "
                    f"{self.doc_volume_cap_bytes} — built on the free AST tier"
                )
                logger.warning("graphify: %s", err)
            elif not self.preflight or self._preflight_ok():
                try:
                    argv = ["extract", "."]
                    argv += [flag for flag in self.extract_flags.split() if flag]
                    argv += ["--backend", self.backend]  # C1: NEVER extract without --backend
                    if self.model:
                        argv += ["--model", self.model]
                    extract_stdout = self._run(argv, checkout_path, self.extract_timeout, with_key=True)
                    # extract writes ONLY graph.json; GRAPH_REPORT.md and named
                    # communities come from cluster-only (verified on 0.8.36 —
                    # without this step every enriched build pays the LLM pass
                    # and stores nothing). cluster-only takes --backend only in
                    # = form; the key rides the env so labeling can run.
                    cluster_argv = ["cluster-only", ".", "--no-viz", f"--backend={self.backend}"]
                    self._run(cluster_argv, checkout_path, self.cluster_timeout, with_key=True)
                    tier = "enriched"
                except PipelineError as exc:
                    if not self.fallback_to_ast:
                        raise
                    err = str(exc)[:500]
                    logger.warning("graphify: enriched tier failed, falling back to AST: %s", err)
            else:
                err = f"preflight failed for backend {self.backend!r} (bad key or no credits)"
                if not self.fallback_to_ast:
                    raise PipelineError(err)
                logger.warning("graphify: %s — falling back to AST", err)
        if tier == "enriched" and not self._report_ok(checkout_path):
            # Belt-and-braces against engine drift: both enriched commands
            # "succeeded" yet no usable report exists. The LLM spend is sunk
            # either way — salvage a working AST build instead of failing.
            err = err or "enriched run produced no usable GRAPH_REPORT.md — degraded to AST"
            logger.warning("graphify: %s", err)
            tier = "ast"
        if tier == "ast":
            self._run(["update", "."], checkout_path, self.update_timeout, with_key=False)
        return self._read_artifacts(checkout_path, tier, err, extract_stdout)

    # ------------------------------------------------------------ subprocess
    def _minimal_env(self, with_key: bool) -> dict:
        """Scrubbed env: PATH/HOME/locale/tmp + ONLY the intended key var.

        Never ``**os.environ`` — stray ambient keys (GOOGLE_API_KEY, AWS_*)
        would hijack graphify's backend auto-detection.
        """
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        if with_key and self.backend:
            key_var = KEY_ENV.get(self.backend)
            if key_var and self.api_key:
                env[key_var] = self.api_key
        return env

    def _run(self, argv: list, cwd: str, timeout: int, with_key: bool) -> str:
        env = self._minimal_env(with_key)
        try:
            proc = subprocess.run(  # nosec B603 B607 — fixed argv, shell=False
                [self.graphify_bin, *argv],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise PipelineError(
                f"graphify binary {self.graphify_bin!r} not found on PATH — install the "
                "[engine] extra (pip install 'aisquare-pipe-graphify[engine]') or pipx "
                "install graphifyy==0.8.36"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            raise PipelineError(
                f"graphify {argv[0]} timed out after {timeout}s; stderr tail: {stderr[-800:]}"
            ) from exc
        if proc.returncode != 0:
            raise PipelineError(
                f"graphify {argv[0]} exited {proc.returncode}; "
                f"stderr tail: {(proc.stderr or '')[-800:]}"
            )
        return proc.stdout or ""

    # ------------------------------------------------------------- preflight
    def _enrichment_armed(self) -> bool:
        if not self.backend:
            return False
        key_var = KEY_ENV.get(self.backend)
        return key_var is None or bool(self.api_key)  # keyless backends (ollama) always armed

    def _preflight_ok(self) -> bool:
        """Cheap auth/credit probe (claude only). Best-effort: network flakiness
        never blocks the build — only a definitive auth/credit failure does."""
        if self.backend != "claude" or not self.api_key:
            return True
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=_PREFLIGHT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — probe is advisory
            logger.warning("graphify: preflight probe errored (%s) — proceeding", exc)
            return True
        if resp.status_code in (401, 403):
            return False
        if resp.status_code == 400 and "credit" in (resp.text or "").lower():
            return False
        return True

    # ----------------------------------------------------- incremental state
    # What survives between builds: the manifest (file inventory), the prior
    # graph (update's diff base), the analysis sidecar, and the content-hash
    # caches (unchanged docs never re-hit the LLM — portable across checkouts
    # by design, graphify #777). Everything lives under graphify-out/.
    _STATE_MEMBERS = ("manifest.json", "graph.json", ".graphify_analysis.json", "cache")

    def _restore_state(self, checkout_path: str, prior_state: bytes) -> None:
        """Unpack a prior build's graphify-out state into the fresh checkout.

        Best-effort: a corrupt/hostile archive degrades to a full (stateless)
        build, never fails it. Members are confined to ``graphify-out/`` and
        extracted with the ``data`` filter (no traversal, no specials).
        """
        try:
            with tarfile.open(fileobj=io.BytesIO(prior_state), mode="r:gz") as tar:
                members = [
                    m
                    for m in tar.getmembers()
                    if m.name.startswith(f"{_OUT_DIRNAME}/") or m.name == _OUT_DIRNAME
                ]
                tar.extractall(checkout_path, members=members, filter="data")
            logger.info("graphify: restored prior incremental state (%d members)", len(members))
        except Exception as exc:  # noqa: BLE001 — stateless build beats a failed one
            logger.warning("graphify: could not restore prior state (full build instead): %s", exc)

    def _capture_state(self, checkout_path: str) -> bytes:
        """Tar this build's graphify-out state for the next build. b"" on miss."""
        out_dir = os.path.join(checkout_path, _OUT_DIRNAME)
        try:
            buffer = io.BytesIO()
            added = 0
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                for member in self._STATE_MEMBERS:
                    path = os.path.join(out_dir, member)
                    if os.path.exists(path):
                        tar.add(path, arcname=f"{_OUT_DIRNAME}/{member}")
                        added += 1
            return buffer.getvalue() if added else b""
        except Exception as exc:  # noqa: BLE001 — state capture must never fail the build
            logger.warning("graphify: could not capture incremental state: %s", exc)
            return b""

    # ----------------------------------------------------------- doc preflight
    def _doc_volume(self, checkout_path: str) -> int:
        """Total bytes of doc-class files — the only content the LLM sees."""
        total = 0
        for dirpath, dirnames, filenames in os.walk(checkout_path):
            dirnames[:] = [d for d in dirnames if d not in _DOC_SCAN_SKIP_DIRS]
            for name in filenames:
                if name.lower().endswith(DOC_EXTENSIONS):
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        continue
        return total

    # ------------------------------------------------------------- artifacts
    def _report_ok(self, checkout_path: str) -> bool:
        report_path = os.path.join(checkout_path, _OUT_DIRNAME, _REPORT_NAME)
        return os.path.isfile(report_path) and os.path.getsize(report_path) > 0

    def _parse_llm_telemetry(self, extract_stdout: str, out_dir: str) -> tuple[int, int, float]:
        """(tokens_in, tokens_out, cost_estimate) — stdout first, analysis-json
        fallback, zeros on a total miss. Never raises: telemetry is observability,
        not correctness."""
        tokens_in = tokens_out = 0
        cost = 0.0
        match = _LLM_TOKENS_RE.search(extract_stdout or "")
        if match:
            try:
                tokens_in = int(match.group(1).replace(",", ""))
                tokens_out = int(match.group(2).replace(",", ""))
            except (TypeError, ValueError):
                tokens_in = tokens_out = 0
        cost_match = _LLM_COST_RE.search(extract_stdout or "")
        if cost_match:
            try:
                cost = float(cost_match.group(1).replace(",", ""))
            except (TypeError, ValueError):
                cost = 0.0
        if not tokens_in and not tokens_out:
            try:
                with open(os.path.join(out_dir, _ANALYSIS_NAME), "r", encoding="utf-8") as fh:
                    tokens = (json.load(fh) or {}).get("tokens") or {}
                tokens_in = int(tokens.get("input") or 0)
                tokens_out = int(tokens.get("output") or 0)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return tokens_in, tokens_out, cost

    def _read_artifacts(
        self, checkout_path: str, tier: str, err: str | None, extract_stdout: str = ""
    ) -> GraphArtifacts:
        out_dir = os.path.join(checkout_path, _OUT_DIRNAME)
        report_path = os.path.join(out_dir, _REPORT_NAME)
        if not os.path.isfile(report_path) or os.path.getsize(report_path) == 0:
            raise PipelineError(
                f"graphify wrote no usable {_REPORT_NAME} (tier={tier}) — "
                "version drift or unsupported content; not storing a corrupt graph"
            )
        with open(report_path, "r", encoding="utf-8", errors="replace") as fh:
            report_md = fh.read()
        graph_json = ""
        graph_path = os.path.join(out_dir, _GRAPH_JSON_NAME)
        if os.path.isfile(graph_path):
            with open(graph_path, "r", encoding="utf-8", errors="replace") as fh:
                graph_json = fh.read()
            try:
                json.loads(graph_json)  # sanity gate: a corrupt JSON must not reach storage
            except (ValueError, TypeError) as exc:
                raise PipelineError(f"graphify wrote unparseable {_GRAPH_JSON_NAME}: {exc}") from exc
        nodes, edges, communities = parse_graph_stats(report_md, graph_json)
        # Telemetry is recorded even when the build degraded after extract —
        # that spend is sunk and is exactly what ops needs to see.
        llm_in, llm_out, llm_cost = self._parse_llm_telemetry(extract_stdout, out_dir)
        return GraphArtifacts(
            report_md=report_md,
            graph_json=graph_json,
            nodes=nodes,
            edges=edges,
            communities=communities,
            token_count=count_tokens(report_md),
            tier=tier,
            enrichment_error=err,
            graphify_version=self._version(),
            llm_tokens_in=llm_in,
            llm_tokens_out=llm_out,
            llm_cost_estimate=llm_cost,
            state_tar=self._capture_state(checkout_path),
        )

    def _version(self) -> str:
        try:
            proc = subprocess.run(  # nosec B603 B607
                [self.graphify_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT,
            )
            return (proc.stdout or proc.stderr or "").strip() or "unknown"
        except Exception:  # noqa: BLE001
            return "unknown"
