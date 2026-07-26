"""Inbound Slack request verification (v0 signing scheme).

Slack signs every Events API / interactivity request:

    X-Slack-Signature:          v0=<hex hmac-sha256 of "v0:<ts>:<raw body>">
    X-Slack-Request-Timestamp:  <unix seconds>

The HOST owns the HTTP route, signing-secret storage, the ``url_verification``
challenge handshake, and the replay/dedup ledger; these pure helpers only
verify bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
TIMESTAMP_WINDOW_SECONDS = 300


def verify_slack_signature(
    raw_body: bytes, timestamp_header: str, signature_header: str, signing_secret: str
) -> bool:
    """Constant-time ``v0=<hex>`` HMAC check over ``v0:<ts>:<raw body>``."""
    if not timestamp_header or not signature_header or not signing_secret:
        return False
    basestring = b"v0:" + timestamp_header.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def timestamp_in_window(timestamp_header: str, *, now: float | None = None) -> bool:
    try:
        ts = float(timestamp_header)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= TIMESTAMP_WINDOW_SECONDS
