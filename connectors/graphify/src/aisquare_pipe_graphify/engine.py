"""GraphifyEngine — one implementation behind both connector faces.

Two-tier build (verified against graphifyy 0.8.36):

* **enriched** — ``graphify extract . --no-viz --backend <b>`` with the
  backend's key in a scrubbed env. NEVER runs without an explicit
  ``--backend``: a backend-less ``extract`` does NOT degrade to a free AST
  pass — it auto-detects a backend from ambient env vars and crashes when
  that backend's package/key is missing.
* **ast** — ``graphify update .`` — the version-correct keyless path
  ("re-extract code files and update the graph (no LLM needed)"). Full
  structural graph (nodes/edges/communities), no LLM-inferred edges, no
  community naming.

Enriched failures fall back to a *working* AST graph in the same run (the
failure tail rides along as ``enrichment_error``) instead of erroring the
whole build — a dead key should degrade the product, not kill it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # nosec B404 — fixed argv, shell=False throughout
from dataclasses import dataclass

import requests

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_graphify.constants import (
    DEFAULT_EXTRACT_FLAGS,
    DEFAULT_EXTRACT_TIMEOUT,
    DEFAULT_UPDATE_TIMEOUT,
    KEY_ENV,
)

logger = logging.getLogger("aisquare.pipe.graphify")

_OUT_DIRNAME = "graphify-out"
_REPORT_NAME = "GRAPH_REPORT.md"
_GRAPH_JSON_NAME = "graph.json"
_VERSION_TIMEOUT = 15
_PREFLIGHT_TIMEOUT = 15

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


class GraphifyEngine:
    def __init__(
        self,
        backend: str | None = None,
        api_key: str | None = None,
        *,
        extract_flags: str = DEFAULT_EXTRACT_FLAGS,
        extract_timeout: int = DEFAULT_EXTRACT_TIMEOUT,
        update_timeout: int = DEFAULT_UPDATE_TIMEOUT,
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
        self.extract_flags = extract_flags or ""
        self.extract_timeout = int(extract_timeout)
        self.update_timeout = int(update_timeout)
        self.graphify_bin = graphify_bin or "graphify"
        self.preflight = bool(preflight)
        self.fallback_to_ast = bool(fallback_to_ast)

    @classmethod
    def from_config(cls, config: dict) -> "GraphifyEngine":
        config = config or {}
        return cls(
            backend=config.get("backend"),
            api_key=config.get("api_key"),
            extract_flags=config.get("extract_flags", DEFAULT_EXTRACT_FLAGS),
            extract_timeout=config.get("extract_timeout_seconds", DEFAULT_EXTRACT_TIMEOUT),
            update_timeout=config.get("update_timeout_seconds", DEFAULT_UPDATE_TIMEOUT),
            graphify_bin=config.get("graphify_bin", "graphify"),
            preflight=config.get("preflight", True),
            fallback_to_ast=config.get("fallback_to_ast", True),
        )

    # ----------------------------------------------------------------- build
    def build(self, checkout_path: str) -> GraphArtifacts:
        tier, err = "ast", None
        if self._enrichment_armed():
            if not self.preflight or self._preflight_ok():
                try:
                    argv = ["extract", "."]
                    argv += [flag for flag in self.extract_flags.split() if flag]
                    argv += ["--backend", self.backend]  # C1: NEVER extract without --backend
                    self._run(argv, checkout_path, self.extract_timeout, with_key=True)
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
        if tier == "ast":
            self._run(["update", "."], checkout_path, self.update_timeout, with_key=False)
        return self._read_artifacts(checkout_path, tier, err)

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

    # ------------------------------------------------------------- artifacts
    def _read_artifacts(self, checkout_path: str, tier: str, err: str | None) -> GraphArtifacts:
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
