"""Inbound webhook verification (shared-secret HMAC).

Salesforce outbound callouts can't sign requests natively, so the host-side
Apex/Flow callout is authored to send:

    X-Pipe-Signature:  sha256=<hex hmac of the raw body, key = shared secret>
    X-Pipe-Timestamp:  <unix seconds>

The HOST owns the HTTP route, secret storage, and replay ledger; these pure
helpers only verify bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Pipe-Signature"
TIMESTAMP_HEADER = "X-Pipe-Timestamp"
TIMESTAMP_WINDOW_SECONDS = 300


def verify_webhook_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Constant-time ``sha256=<hex>`` HMAC check over the raw request body."""
    if not signature_header or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def timestamp_in_window(timestamp_header: str, *, now: float | None = None) -> bool:
    try:
        ts = float(timestamp_header)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= TIMESTAMP_WINDOW_SECONDS
