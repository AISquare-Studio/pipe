"""DocuSign Connect webhook verification (official HMAC scheme).

``X-DocuSign-Signature-1`` = base64(HMAC-SHA256(raw body, connect key)).
The HOST owns the HTTP route, key storage, and replay ledger; this pure
helper only verifies bytes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

SIGNATURE_HEADER = "X-DocuSign-Signature-1"


def verify_connect_hmac(raw_body: bytes, signature_header: str, key: str) -> bool:
    if not signature_header or not key:
        return False
    digest = hmac.new(key.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature_header.strip())
