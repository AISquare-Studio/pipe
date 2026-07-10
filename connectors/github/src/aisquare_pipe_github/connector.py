"""GitHubRepoSource — shallow-clone one GitHub repo, emit a checkout handle."""

from __future__ import annotations

import logging

from aisquare.pipe import AuthType, DataEnvelope, MetaField, SourceConnector

from aisquare_pipe_github.client import GitHubRepoClient
from aisquare_pipe_github.constants import CHECKOUT_CONTENT_TYPE

logger = logging.getLogger("aisquare.pipe.github")


class GitHubRepoSource(SourceConnector):
    """Shallow-clone one GitHub repo; emit ONE checkout-handle envelope.

    The envelope's ``data["path"]`` points at an on-disk checkout that exists
    only while this generator is suspended (i.e. for the converter/sink
    processing this envelope in the same ``Pipeline.run``). Set
    ``keep_checkout``/``checkout_dir`` to retain it. Token kinds: GitHub App
    installation token or PAT — both ride the same Basic ``x-access-token``
    extraheader, injected via ``GIT_CONFIG_*`` env, never argv.
    """

    name = "github"  # config-dict key (class attr, NOT the entry-point name)
    version = "0.1.0"
    output_types = [CHECKOUT_CONTENT_TYPE]
    auth_type = AuthType.API_KEY
    description = "Single-repo shallow checkout source for GitHub."

    CONFIG_SPEC = {
        "full_name": MetaField(type=str, required=True, description="owner/name"),
        "token": MetaField(
            type=str, description="installation token or PAT; omit for public repos"
        ),
        "default_branch": MetaField(type=str, default="main"),
        "skip_if_sha": MetaField(
            type=str,
            description="yield nothing when remote HEAD == this sha (pre-clone ls-remote)",
        ),
        "fetch_repo_metadata": MetaField(
            type=bool,
            default=False,
            description="best-effort GET /repos/<full_name> for description/language",
        ),
        "clone_timeout_seconds": MetaField(type=int, default=300),
        "keep_checkout": MetaField(type=bool, default=False),
        "checkout_dir": MetaField(
            type=str, description="clone here instead of a tempdir (implies keep_checkout)"
        ),
    }
    metadata_spec = {
        "repo_full_name": MetaField(type=str, required=True, description="owner/name"),
        "head_sha": MetaField(
            type=str, required=True, description="git rev-parse HEAD of the checkout"
        ),
        "default_branch": MetaField(type=str),
        "description": MetaField(
            type=str, description="GitHub repo description (when fetch_repo_metadata)"
        ),
        "language": MetaField(
            type=str, description="GitHub primary language (when fetch_repo_metadata)"
        ),
    }

    def pull(self, config, params=None):
        config = config or {}
        client = GitHubRepoClient(config)  # ConfigValidationError on missing/bad full_name
        skip_sha = config.get("skip_if_sha") or ""
        if skip_sha and client.ls_remote_head() == skip_sha:
            logger.info(
                "github: %s unchanged at %s — skipping", client.full_name, skip_sha[:12]
            )
            return
        workdir, owned = client.make_workdir()
        try:
            checkout = client.clone(workdir)  # PipelineError w/ stderr tail on failure
            head_sha = client.rev_parse_head(checkout)  # "" on failure
            meta = {
                "repo_full_name": client.full_name,
                "head_sha": head_sha,
                "default_branch": client.branch,
            }
            if config.get("fetch_repo_metadata"):
                meta.update(client.repo_metadata())  # best-effort {} on any error
            yield DataEnvelope(
                content_type=CHECKOUT_CONTENT_TYPE,
                data={"path": str(checkout), "head_sha": head_sha},
                source_id=f"github:{client.full_name}@{head_sha[:12] or 'unknown'}",
                metadata=meta,
            )
        finally:
            # Runs when the pipeline advances/closes the generator — i.e. after
            # the converter+sink finished with this envelope's checkout.
            if owned and not config.get("keep_checkout"):
                GitHubRepoClient.cleanup(workdir)

    def validate_config(self, config):
        try:
            return GitHubRepoClient.shape_ok(config)  # shape-only; no network
        except Exception:  # noqa: BLE001
            return False
