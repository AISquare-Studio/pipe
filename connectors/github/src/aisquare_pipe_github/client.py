"""Git/HTTP client for the GitHub checkout source.

Ported from AISquare-Studio-BE ``integrations_github/tasks.py`` with identical
auth semantics: the token rides an ``http.extraHeader`` Basic header injected
via ``GIT_CONFIG_*`` env vars — never argv (world-readable /proc/cmdline),
never the remote URL (leaks into captured stderr on a failed clone). Works the
same for GitHub App installation tokens and personal access tokens.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess  # nosec B404 — fixed argv, shell=False throughout
import tempfile

import requests

from aisquare.pipe.errors import ConfigValidationError, PipelineError

logger = logging.getLogger("aisquare.pipe.github")

_FULL_NAME_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_LS_REMOTE_TIMEOUT = 30
_REV_PARSE_TIMEOUT = 30
_METADATA_TIMEOUT = 10
_DEFAULT_CLONE_TIMEOUT = 300


class GitHubRepoClient:
    """One repo, one optional token, a handful of subprocess/HTTP helpers."""

    def __init__(self, config: dict) -> None:
        config = config or {}
        full_name = (config.get("full_name") or "").strip()
        if not _FULL_NAME_RE.match(full_name):
            raise ConfigValidationError(
                "github config requires 'full_name' shaped like 'owner/name' "
                f"(got {full_name!r})"
            )
        self.full_name: str = full_name
        self.branch: str = (config.get("default_branch") or "main").strip() or "main"
        self.token: str = config.get("token") or ""
        self.clone_timeout: int = int(config.get("clone_timeout_seconds") or _DEFAULT_CLONE_TIMEOUT)
        self._checkout_dir_cfg: str = config.get("checkout_dir") or ""

    # ------------------------------------------------------------------ shape
    @staticmethod
    def shape_ok(config: dict) -> bool:
        """Offline config check (no network): full_name present and well-shaped."""
        try:
            return bool(_FULL_NAME_RE.match((config or {}).get("full_name") or ""))
        except Exception:  # noqa: BLE001 — validate_config must never raise
            return False

    # ------------------------------------------------------------------- auth
    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.full_name}.git"

    def _auth_env(self) -> dict:
        """Subprocess env: token via http.extraHeader (GIT_CONFIG_*), never argv."""
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",  # fail fast, never block on a prompt
        }
        if self.token:
            auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraHeader",
                GIT_CONFIG_VALUE_0=f"Authorization: Basic {auth}",
            )
        return env

    # ------------------------------------------------------------------- git
    def ls_remote_head(self) -> str:
        """Remote HEAD sha of the branch — '' on any failure (skip check is
        best-effort; a miss just means we proceed to clone)."""
        try:
            result = subprocess.run(  # nosec B603 B607
                ["git", "ls-remote", self.clone_url, f"refs/heads/{self.branch}"],
                capture_output=True,
                text=True,
                timeout=_LS_REMOTE_TIMEOUT,
                env=self._auth_env(),
            )
            if result.returncode != 0:
                return ""
            first = (result.stdout or "").split()
            return first[0] if first else ""
        except Exception:  # noqa: BLE001
            return ""

    def make_workdir(self) -> tuple[str, bool]:
        """(workdir, owned): a tempdir we own, unless checkout_dir pins one."""
        if self._checkout_dir_cfg:
            os.makedirs(self._checkout_dir_cfg, exist_ok=True)
            return self._checkout_dir_cfg, False
        return tempfile.mkdtemp(prefix="pipe-github-"), True

    def clone(self, workdir: str) -> str:
        """Shallow single-branch clone into <workdir>/checkout; returns the path."""
        checkout = os.path.join(workdir, "checkout")
        try:
            result = subprocess.run(  # nosec B603 B607
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--branch",
                    self.branch,
                    self.clone_url,
                    checkout,
                ],
                capture_output=True,
                text=True,
                timeout=self.clone_timeout,
                env=self._auth_env(),
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            raise PipelineError(
                f"git clone of {self.full_name} timed out after {self.clone_timeout}s; "
                f"stderr tail: {stderr[-800:]}"
            ) from exc
        if result.returncode != 0:
            raise PipelineError(
                f"git clone of {self.full_name} exited {result.returncode}; "
                f"stderr tail: {(result.stderr or '')[-800:]}"
            )
        return checkout

    def rev_parse_head(self, checkout: str) -> str:
        """HEAD sha of the checkout — '' on failure (freshness nicety)."""
        try:
            result = subprocess.run(  # nosec B603 B607
                ["git", "-C", checkout, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=_REV_PARSE_TIMEOUT,
            )
            if result.returncode != 0:
                return ""
            return (result.stdout or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------- http
    def repo_metadata(self) -> dict:
        """Best-effort GET /repos/<full_name> → {description, language}; {} on any error."""
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{self.full_name}",
                headers=headers,
                timeout=_METADATA_TIMEOUT,
            )
            if resp.status_code != 200:
                return {}
            payload = resp.json()
            meta = {}
            if payload.get("description"):
                meta["description"] = str(payload["description"])
            if payload.get("language"):
                meta["language"] = str(payload["language"])
            return meta
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def cleanup(workdir: str) -> None:
        shutil.rmtree(workdir, ignore_errors=True)
