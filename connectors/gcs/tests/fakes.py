"""A fake Google: the OAuth token endpoint and the Cloud Storage JSON API.

``FakeGoogle`` is a ``requests.Session`` stand-in: the toolkit, the source and
the token exchange all call ``session.request(...)``. The token endpoint
decodes the signed JWT assertion (no signature check: a real key is never
used) and records who asked; the storage API serves an in-memory bucket with
prefix, delimiter, startOffset and page-token listing, metadata and
``alt=media`` reads. Every request is recorded; a wrong bearer token is a 401.

:func:`real_session` puts the same fake behind a REAL ``requests.Session``
(an ``HTTPAdapter`` whose ``send`` answers with urllib3 responses), so a body
that breaks off midway fails exactly as it would off a socket: urllib3 and
requests wrap the error. No socket is opened.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

import requests
import requests.adapters
import urllib3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

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


def ec_private_key_pem() -> str:
    """A throwaway P-256 key: PEM-shaped, but not a key Google signs with."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


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
    #: (after, error): the body breaks off after that many bytes with that
    #: socket-level error. Only real_session() can play it.
    broken: tuple[int, BaseException] | None = None

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
        assert self.broken is None, "a broken body needs real_session()"
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
    content_encoding: str | None = None

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
        if self.content_encoding is not None:
            resource["contentEncoding"] = self.content_encoding
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
        #: The token endpoint answers (status, body[, headers]) instead.
        self.token_error: tuple[Any, ...] | None = None
        #: The token request raises this (as requests would).
        self.token_raise: BaseException | None = None
        #: The token answer's body breaks off with this (real_session only).
        self.token_body_break: BaseException | None = None
        #: Every storage call answers (status, body, headers); "{token}" in the
        #: body's text is replaced by the request's bearer token (an echo).
        self.fail_with: tuple[int, dict[str, Any], dict[str, str]] | None = None
        self.raise_timeout = False
        #: Every bearer token is refused, even one just minted.
        self.reject_all_tokens = False
        #: alt=media bodies break off after ``break_after`` bytes with this.
        self.media_break: BaseException | None = None
        self.break_after = 4

    # -- the requests.Session surface ------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if url == TOKEN_URL:
            return self._token(kwargs)
        if self.raise_timeout:
            raise requests.Timeout("timed out")
        auth = (kwargs.get("headers") or {}).get("Authorization", "")
        if (
            not auth.startswith("Bearer ")
            or auth[7:] not in self.tokens
            or self.reject_all_tokens
        ):
            return FakeResponse(
                401, {"error": {"code": 401, "message": "Invalid Credentials"}}
            )
        if self.fail_with is not None:
            status, body, headers = self.fail_with
            echoed = json.loads(json.dumps(body).replace("{token}", auth[7:]))
            return FakeResponse(status, echoed, headers=dict(headers))
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
                200,
                content=stored.data,
                headers={"Content-Type": stored.content_type},
                broken=(self.break_after, self.media_break)
                if self.media_break is not None
                else None,
            )
        return FakeResponse(200, stored.resource(self.bucket, name))

    def close(self) -> None:
        pass

    def revoke_tokens(self) -> None:
        """Every token minted so far stops working (key or account disabled)."""
        self.tokens.clear()

    # -- endpoints -----------------------------------------------------------------------

    def _token(self, kwargs: dict[str, Any]) -> FakeResponse:
        body = kwargs.get("data") or b""
        form = parse_qs(body.decode() if isinstance(body, bytes) else str(body))
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
        claims = _jwt_claims(form["assertion"][0])
        self.token_requests.append(claims)
        if self.token_raise is not None:
            raise self.token_raise
        if self.token_error is not None:
            status, error, *headers = self.token_error
            return FakeResponse(
                status, error, headers=dict(headers[0] if headers else {})
            )
        token = f"ya29.fake-token-{len(self.token_requests)}"
        self.tokens.add(token)
        return FakeResponse(
            200,
            {"access_token": token, "expires_in": 3600, "token_type": "Bearer"},
            broken=(2, self.token_body_break)
            if self.token_body_break is not None
            else None,
        )

    def _list(self, params: dict[str, Any]) -> FakeResponse:
        prefix = str(params.get("prefix") or "")
        size = int(params.get("maxResults") or 1000)
        start = int(params.get("pageToken") or 0)
        offset = str(params.get("startOffset") or "")
        names = sorted(n for n in self.objects if n.startswith(prefix) and n >= offset)
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


class _Body(io.RawIOBase):
    """A socket-like body: ``data``, or its first ``after`` bytes and then
    ``error`` (``TimeoutError``, ``ConnectionResetError``...)."""

    def __init__(
        self, data: bytes, broken: tuple[int, BaseException] | None = None
    ) -> None:
        self._data = data
        self._pos = 0
        self._broken = broken

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        end = len(self._data)
        if self._broken is not None:
            after, error = self._broken
            if self._pos >= after:
                raise error
            end = min(end, after)
        n = min(len(buffer), end - self._pos)
        buffer[:n] = self._data[self._pos : self._pos + n]
        self._pos += n
        return n


class _FakeGoogleAdapter(requests.adapters.HTTPAdapter):
    """Answers every request from a FakeGoogle with a urllib3 response."""

    def __init__(self, google: FakeGoogle) -> None:
        super().__init__()
        self._google = google

    def send(self, request: Any, stream: bool = False, timeout: Any = None, **_: Any):
        url, _sep, query = str(request.url).partition("?")
        params = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
        answer = self._google.request(
            str(request.method),
            url,
            params=params,
            data=request.body,
            headers=dict(request.headers),
            timeout=timeout,
            allow_redirects=False,
            stream=stream,
        )
        raw = urllib3.HTTPResponse(
            body=_Body(answer.content, answer.broken),
            headers=answer.headers,
            status=answer.status_code,
            preload_content=False,
            request_method=str(request.method),
        )
        return self.build_response(request, raw)


def real_session(google: FakeGoogle) -> requests.Session:
    """A real ``requests.Session`` whose HTTPS goes to ``google``. It ignores
    proxy settings (a proxy lookup may resolve names on macOS)."""
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", _FakeGoogleAdapter(google))
    return session
