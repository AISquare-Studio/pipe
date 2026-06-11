"""HTTP client wrapper for the n8n Executions API."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

from aisquare.pipe.errors import ConfigValidationError, PipelineError

logger = logging.getLogger("aisquare.pipe.n8n")


class N8nClient:
    """Thin wrapper around n8n's REST API.

    All HTTP errors are translated to PipelineError so the framework can
    surface them through PipelineResult.errors.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        url = config.get("n8n_url")
        key = config.get("api_key")
        if not url or not isinstance(url, str):
            raise ConfigValidationError(
                "n8n config requires 'n8n_url' (base URL of the n8n instance)"
            )
        if not key or not isinstance(key, str):
            raise ConfigValidationError(
                "n8n config requires 'api_key' (n8n API key)"
            )
        self._base_url = url.rstrip("/")
        self._headers = {"X-N8N-API-KEY": key, "Accept": "application/json"}
        self._timeout = int(config.get("request_timeout_seconds", 30))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            resp = requests.get(
                url, headers=self._headers, params=params, timeout=self._timeout
            )
        except requests.RequestException as e:
            raise PipelineError(f"n8n request to {path} failed: {e}") from e
        if resp.status_code >= 400:
            raise PipelineError(
                f"n8n {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise PipelineError(
                f"n8n {path} returned non-JSON response: {resp.text[:200]}"
            ) from e

    def list_executions(
        self,
        last_id: int = 0,
        workflow_ids: list[str] | None = None,
        include_data: bool = True,
        limit: int = 100,
        finished_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Return executions newer than last_id, sorted ascending by id.

        n8n's API returns the most recent first; we sort ascending so the
        cursor advances monotonically. When ``finished_only`` is True (the
        default) we drop in-progress runs so the cursor never advances
        past an unfinished execution — those are handled separately by
        :meth:`list_running_executions`.
        """
        # n8n's API does not support a lastId query parameter. We over-fetch
        # using `limit` and then filter client-side. For v1 demo loads this
        # is fine; cursor-based pagination is a follow-up.
        params: dict[str, Any] = {
            "limit": limit,
            "includeData": "true" if include_data else "false",
        }

        if workflow_ids:
            collected: list[dict[str, Any]] = []
            for wid in workflow_ids:
                page = self._get(
                    "/api/v1/executions", {**params, "workflowId": wid}
                )
                collected.extend(page.get("data", []))
            executions = collected
        else:
            page = self._get("/api/v1/executions", params)
            executions = page.get("data", [])

        if finished_only:
            executions = [e for e in executions if e.get("finished")]

        # Filter strictly greater than last_id (n8n's lastId may be inclusive).
        if last_id:
            executions = [
                e for e in executions if int(e.get("id", 0)) > last_id
            ]

        executions.sort(key=lambda e: int(e.get("id", 0)))
        return executions

    def list_running_executions(
        self,
        workflow_ids: list[str] | None = None,
        include_data: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch in-progress executions. With ``include_data=True``, n8n
        returns the runData accumulated so far — used to emit progressive
        trace updates as nodes complete inside a still-running workflow.
        """
        base_params: dict[str, Any] = {
            "limit": limit,
            "status": "running",
            "includeData": "true" if include_data else "false",
        }

        if workflow_ids:
            collected: list[dict[str, Any]] = []
            for wid in workflow_ids:
                page = self._get(
                    "/api/v1/executions", {**base_params, "workflowId": wid}
                )
                collected.extend(page.get("data", []))
        else:
            page = self._get("/api/v1/executions", base_params)
            collected = page.get("data", [])

        # Defensive: n8n's status filter is honoured on supported versions;
        # we still strip any finished executions that slip through.
        return [e for e in collected if not e.get("finished")]

    def get_workflow_definition(self, workflow_id: str) -> dict[str, Any] | None:
        """Return ``{name, nodes: [...], ...}`` or None when the workflow
        isn't reachable. Used to enrich stub traces with the workflow's
        true name + node list."""
        try:
            return self._get(f"/api/v1/workflows/{workflow_id}")
        except PipelineError as e:
            logger.warning(
                "n8n get_workflow_definition(%s) failed: %s — returning None",
                workflow_id, e,
            )
            return None

    def list_workflows(self) -> list[dict[str, Any]]:
        """Cheap call used to validate credentials."""
        page = self._get("/api/v1/workflows", {"limit": 1})
        return page.get("data", [])

    def validate(self) -> bool:
        """Probe /api/v1/workflows to confirm the API key works."""
        self.list_workflows()
        return True


CURSOR_FILENAME = "n8n-cursor.json"
# Pre-0.2.1 default, kept only as a one-time migration source.
LEGACY_CURSOR_PATH = "/tmp/n8n-pipe-cursor.json"


def default_state_dir() -> Path:
    """Per-user state root: ``$XDG_CACHE_HOME/aisquare-pipe`` when set, else
    ``~/.cache/aisquare-pipe``. Deliberately not a shared tempdir — fixed
    names under world-writable ``/tmp`` collide across users and expose
    cursor state."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "aisquare-pipe"


def default_cursor_path() -> str:
    """Per-user default location of the execution cursor file."""
    return str(default_state_dir() / CURSOR_FILENAME)


def migrate_legacy_cursor(legacy_path: str, new_path: str) -> None:
    """One-time copy of a pre-0.2.1 shared-``/tmp`` cursor file into the
    per-user state dir. Runs only while the new file does not exist; the
    legacy file is left in place for instances still on older versions."""
    legacy, new = Path(legacy_path), Path(new_path)
    try:
        if new.exists() or not legacy.exists():
            return
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(legacy.read_bytes())
        logger.info("Migrated execution cursor from %s to %s", legacy, new)
    except OSError as e:
        logger.warning("Could not migrate legacy cursor %s: %s", legacy_path, e)


def load_cursor(path: str) -> int:
    """Read the last-seen execution id from disk, defaulting to 0."""
    p = Path(path)
    if not p.exists():
        return 0
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Failed to read cursor at %s: %s — starting from 0", path, e)
        return 0
    value = payload.get("last_execution_id", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def save_cursor(path: str, last_execution_id: int) -> None:
    """Atomically persist the cursor to disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"last_execution_id": int(last_execution_id)}),
        encoding="utf-8",
    )
    tmp.replace(p)
