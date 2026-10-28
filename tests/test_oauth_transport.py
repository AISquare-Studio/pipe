"""OAuth transport safety against real loopback HTTP servers: redirects are
never followed (client credentials stay put), truncated responses become
OAuthError, and non-https endpoints are refused before any request.

The servers bind 127.0.0.1 only — nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aisquare.pipe import (
    OAuthError,
    OAuthSpec,
    exchange_code,
    refresh_access_token,
    revoke_token,
)

Handler = Callable[[BaseHTTPRequestHandler], None]


@contextmanager
def serve(handle: Handler) -> Iterator[tuple[str, list[dict]]]:
    """A one-route loopback server; yields (base_url, received requests)."""
    received: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length).decode(),
                }
            )
            handle(self)

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()


def _json(status: int, payload: dict) -> Handler:
    def handle(h: BaseHTTPRequestHandler) -> None:
        body = json.dumps(payload).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    return handle


def _redirect(location: str, status: int = 302) -> Handler:
    def handle(h: BaseHTTPRequestHandler) -> None:
        h.send_response(status)
        h.send_header("Location", location)
        h.send_header("Content-Length", "0")
        h.end_headers()

    return handle


def _truncated(h: BaseHTTPRequestHandler) -> None:
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", "500")
    h.end_headers()
    h.wfile.write(b'{"access_token": "par')
    h.wfile.flush()
    h.close_connection = True


def _spec(base: str, **kwargs) -> OAuthSpec:
    return OAuthSpec(
        provider="local",
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        revoke_url=f"{base}/revoke",
        **kwargs,
    )


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_token_redirect_is_not_followed(status):
    stolen = _json(200, {"access_token": "attacker-token", "token_type": "bearer"})
    with serve(stolen) as (thief, thief_seen), serve(
        _redirect(f"{thief}/steal", status)
    ) as (base, provider_seen):
        spec = _spec(base, token_auth_method="client_secret_basic")
        with pytest.raises(OAuthError) as info:
            exchange_code(
                spec,
                client_id="cid",
                client_secret="SUPERSECRET",
                code="the-code",
                redirect_uri="https://app/cb",
            )
    assert (info.value.error, info.value.status) == ("unexpected_redirect", status)
    assert len(provider_seen) == 1
    assert thief_seen == []  # neither credentials nor the code reached the redirect target
    assert "SUPERSECRET" not in str(info.value)


def test_refresh_redirect_is_not_followed():
    with serve(_json(200, {"access_token": "x"})) as (thief, thief_seen), serve(
        _redirect(f"{thief}/steal")
    ) as (base, _), pytest.raises(OAuthError) as info:
        refresh_access_token(_spec(base), client_id="c", client_secret="s", refresh_token="r")
    assert info.value.error == "unexpected_redirect"
    assert thief_seen == []


def test_revoke_redirect_is_not_followed():
    with serve(_json(200, {})) as (thief, thief_seen), serve(_redirect(f"{thief}/steal")) as (base, _):
        assert revoke_token(_spec(base), client_id="c", client_secret="s", token="t") is False
    assert thief_seen == []


def test_real_round_trip_over_loopback():
    with serve(_json(200, {"access_token": "real", "expires_in": 60})) as (base, seen):
        token = exchange_code(
            _spec(base), client_id="c", client_secret="s", code="x", redirect_uri="https://r"
        )
    assert token.access_token == "real"
    assert seen[0]["path"] == "/token"
    assert seen[0]["headers"]["Accept"] == "application/json"


def test_truncated_response_is_oauth_error():
    with serve(_truncated) as (base, _), pytest.raises(OAuthError) as info:
        exchange_code(
            _spec(base), client_id="c", client_secret="s", code="x", redirect_uri="https://r"
        )
    assert info.value.error == "network_error"
    assert "IncompleteRead" in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.example.com/token",
        "http://10.0.0.5/token",
        "ftp://auth.example.com/token",
        "auth.example.com/token",
        "https:///token",
    ],
)
def test_insecure_token_url_refused_before_any_request(url, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("no request may be made to an insecure URL")

    monkeypatch.setattr("urllib.request.build_opener", no_network)
    spec = OAuthSpec(provider="p", authorize_url="https://a.example/auth", token_url=url)
    with pytest.raises(OAuthError) as info:
        exchange_code(spec, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
    assert info.value.error == "insecure_url"
    with pytest.raises(OAuthError):
        refresh_access_token(spec, client_id="c", client_secret="s", refresh_token="r")


def test_insecure_revoke_url_is_false_without_request(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("no request may be made to an insecure URL")

    monkeypatch.setattr("urllib.request.build_opener", no_network)
    spec = OAuthSpec(
        provider="p",
        authorize_url="https://a.example/auth",
        token_url="https://a.example/token",
        revoke_url="http://a.example/revoke",
    )
    assert revoke_token(spec, client_id="c", client_secret="s", token="t") is False


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_http_allowed(host, monkeypatch):
    from aisquare.pipe.core.oauth import _require_secure_url

    _require_secure_url(_spec("https://x"), f"http://{host}:8080/token", "token_url")
