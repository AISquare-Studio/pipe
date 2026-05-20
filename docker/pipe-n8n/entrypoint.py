#!/usr/bin/env python3
"""Entrypoint for the aisquare/pipe-n8n image.

Translates the documented env vars into the JSON config the connectors
expect, writes it to /tmp/pipe-config.json, and execs:

    pipe run --source n8n --sink aisquare-gateway --config /tmp/pipe-config.json

Required env vars cause a clear failure with exit code 1 and a usable
error message on stderr — no stack traces, no silent defaults.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG_PATH = "/tmp/pipe-config.json"

REQUIRED = ("N8N_URL", "N8N_API_KEY", "AISQUARE_GATEWAY_URL", "AISQUARE_API_KEY")


def _die(msg: str) -> "None":
    sys.stderr.write(f"pipe-n8n: {msg}\n")
    sys.exit(1)


def _build_config() -> dict:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        _die(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + ". Required: " + ", ".join(REQUIRED)
        )

    source: dict = {
        "n8n_url": os.environ["N8N_URL"],
        "api_key": os.environ["N8N_API_KEY"],
    }
    if os.environ.get("N8N_POLL_INTERVAL"):
        try:
            source["poll_interval_seconds"] = int(os.environ["N8N_POLL_INTERVAL"])
        except ValueError:
            _die("N8N_POLL_INTERVAL must be an integer (seconds)")

    if os.environ.get("N8N_CURSOR_PATH"):
        source["cursor_path"] = os.environ["N8N_CURSOR_PATH"]

    if os.environ.get("N8N_WORKFLOW_FILTER"):
        source["workflow_id_filter"] = [
            w.strip() for w in os.environ["N8N_WORKFLOW_FILTER"].split(",") if w.strip()
        ]

    sink: dict = {
        "gateway_url": os.environ["AISQUARE_GATEWAY_URL"],
        "api_key": os.environ["AISQUARE_API_KEY"],
    }
    if os.environ.get("AISQUARE_INGEST_PATH"):
        sink["ingest_path"] = os.environ["AISQUARE_INGEST_PATH"]
    if os.environ.get("AISQUARE_TIMEOUT_SECONDS"):
        try:
            sink["timeout_seconds"] = int(os.environ["AISQUARE_TIMEOUT_SECONDS"])
        except ValueError:
            _die("AISQUARE_TIMEOUT_SECONDS must be an integer (seconds)")
    if os.environ.get("AISQUARE_MAX_RETRIES"):
        try:
            sink["max_retries"] = int(os.environ["AISQUARE_MAX_RETRIES"])
        except ValueError:
            _die("AISQUARE_MAX_RETRIES must be an integer")

    return {"n8n": source, "aisquare-gateway": sink}


def main() -> None:
    config = _build_config()
    Path(CONFIG_PATH).write_text(json.dumps(config), encoding="utf-8")

    # Replace this process with `pipe run ...` so signals propagate cleanly
    # and the container's lifecycle matches the pipeline's.
    argv = [
        "pipe",
        "run",
        "--source",
        "n8n",
        "--sink",
        "aisquare-gateway",
        "--config",
        CONFIG_PATH,
    ]
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
