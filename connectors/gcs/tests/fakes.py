"""A fake Google: the OAuth token endpoint and the Cloud Storage JSON API.

``FakeGoogle`` is a ``requests.Session`` stand-in: the toolkit, the source and
google-auth's token refresh all call ``session.request(...)``. The token
endpoint decodes the signed JWT assertion (no signature check: a real key is
never used) and records who asked; the storage API serves an in-memory bucket
with prefix, delimiter and page-token listing, metadata and ``alt=media``
reads. Every request is recorded; a wrong bearer token is a 401.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://storage.googleapis.com/storage/v1/b/"

_KEYS: dict[str, str] = {}


def private_key_pem(label: str = "default") -> str:
    """A throwaway RSA key (made once per label per test run)."""
    if label not in _KEYS:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _KEYS[label] = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    return _KEYS[label]


def service_account(
    email: str = "reader@acme-proj.iam.gserviceaccount.com", label: str = "default"
) -> dict[str, Any]:
    return {
        "type": "service_account",
        "project_id": "acme-proj",
        "private_key_id": f"kid-{label}",
        "private_key": private_key_pem(label),
        "client_email": email,
        "client_id": "1234567890",
        "token_uri": TOKEN_URL,
        "universe_domain": "googleapis.com",
    }


def blob(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "service_account_json": service_account(),
        "bucket": "acme-policies",
        "prefix": "policies/",
    }
    config.update(overrides)
    return config


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.body is not None and not self.content:
            self.content = json.dumps(self.body).encode()
            self.headers.setdefault("Content-Type", "application/json")

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.content)

    def iter_content(self, size: int = 1024):
        for start in range(0, len(self.content), size):
            yield self.content[start : start + size]

    def close(self) -> None:
        pass


@dataclass
class StoredObject:
    data: bytes
    content_type: str = "application/octet-stream"
    generation: str = "1700000000000001"
    updated: str = "2026-11-17T10:00:00.000Z"
    md5_hash: str | None = "XrY7u+Ae7tCTyyK7j1rNww=="

    def resource(self, bucket: str, name: str) -> dict[str, Any]:
        resource = {
            "kind": "storage#object",
            "name": name,
            "bucket": bucket,
            "size": str(len(self.data)),
            "contentType": self.content_type,
            "generation": self.generation,
            "metageneration": "1",
            "crc32c": "yZRlqg==",
            "etag": f"CIG{self.generation[-4:]}=",
            "updated": self.updated,
            "timeCreated": "2026-11-01T10:00:00.000Z",
            "storageClass": "STANDARD",
        }
        if self.md5_hash is not None:
            resource["md5Hash"] = self.md5_hash
        return resource


def _jwt_claims(assertion: str) -> dict[str, Any]:
    payload = assertion.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class FakeGoogle:
    """The session every call goes through (see the module docstring)."""

    def __init__(
        self,
        bucket: str = "acme-policies",
        objects: dict[str, StoredObject] | None = None,
    ) -> None:
        self.bucket = bucket
        self.objects: dict[str, StoredObject] = dict(objects or {})
        self.requests: list[dict[str, Any]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.tokens: set[str] = set()
        self.token_error: tuple[int, dict[str, Any]] | None = None
        self.fail_with: tuple[int, dict[str, Any], dict[str, str]] | None = None
        self.raise_timeout = False

    # -- the requests.Session surface ------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if url == TOKEN_URL:
            return self._token(kwargs)
        if self.raise_timeout:
            import requests

            raise requests.Timeout("timed out")
        auth = (kwargs.get("headers") or {}).get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
            return FakeResponse(
                401, {"error": {"code": 401, "message": "Invalid Credentials"}}
            )
        if self.fail_with is not None:
            status, body, headers = self.fail_with
            return FakeResponse(status, body, headers=dict(headers))
        if not url.startswith(API):
            return FakeResponse(404, {"error": {"message": "not found"}})
        bucket, _, rest = url[len(API) :].partition("/o")
        if unquote(bucket) != self.bucket:
            return FakeResponse(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": "The specified bucket does not exist.",
                    }
                },
            )
        params = dict(kwargs.get("params") or {})
        if rest == "":
            return self._list(params)
        name = unquote(rest.lstrip("/"))
        stored = self.objects.get(name)
        if stored is None or (
            params.get("generation") and params["generation"] != stored.generation
        ):
            return FakeResponse(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": f"No such object: {self.bucket}/{name}",
                    }
                },
            )
        if params.get("alt") == "media":
            return FakeResponse(
                200, content=stored.data, headers={"Content-Type": stored.content_type}
            )
        return FakeResponse(200, stored.resource(self.bucket, name))

    def close(self) -> None:
        pass

    # -- endpoints -----------------------------------------------------------------------

    def _token(self, kwargs: dict[str, Any]) -> FakeResponse:
        body = kwargs.get("data") or b""
        form = parse_qs(body.decode() if isinstance(body, bytes) else str(body))
        claims = _jwt_claims(form["assertion"][0])
        self.token_requests.append(claims)
        if self.token_error is not None:
            status, error = self.token_error
            return FakeResponse(status, error)
        token = f"ya29.fake-token-{len(self.token_requests)}"
        self.tokens.add(token)
        return FakeResponse(
            200, {"access_token": token, "expires_in": 3600, "token_type": "Bearer"}
        )

    def _list(self, params: dict[str, Any]) -> FakeResponse:
        prefix = str(params.get("prefix") or "")
        size = int(params.get("maxResults") or 1000)
        start = int(params.get("pageToken") or 0)
        names = sorted(n for n in self.objects if n.startswith(prefix))
        prefixes: list[str] = []
        if params.get("delimiter") == "/":
            # GCS: a name with a "/" after the prefix rolls up into a sub-prefix.
            direct = []
            for n in names:
                rest = n[len(prefix) :]
                if "/" in rest:
                    sub = prefix + rest.split("/", 1)[0] + "/"
                    if sub not in prefixes:
                        prefixes.append(sub)
                else:
                    direct.append(n)
            names = direct
        page = names[start : start + size]
        body: dict[str, Any] = {"kind": "storage#objects"}
        if page:
            body["items"] = [self.objects[n].resource(self.bucket, n) for n in page]
        if prefixes and start == 0:
            body["prefixes"] = prefixes
        if start + size < len(names):
            body["nextPageToken"] = str(start + size)
        return FakeResponse(200, body)

    # -- what was asked ------------------------------------------------------------------

    def storage_calls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["url"] != TOKEN_URL]
