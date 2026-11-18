"""The credential blob, the key-pair JWT, the OAuth client, and failing closed."""

from __future__ import annotations

import base64
import json
import time

import pytest
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from aisquare_pipe_snowflake import (
    SnowflakeNotConfigured,
    SnowflakeQuerySource,
    SnowflakeToolkit,
    load_credentials,
)
from aisquare_pipe_snowflake.credentials import keypair_jwt
from tests.fakes import (
    ACCOUNT,
    CLIENT_SECRET,
    TOKEN_URL,
    USER,
    Canned,
    FakeResponse,
    FakeSnowflake,
    blob,
    col,
    ec_private_key_pem,
    fingerprint,
    oauth_blob,
    private_key_pem,
    rsa_key,
)


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _select(fake: FakeSnowflake, config: dict, sql: str = "SELECT 1 AS X"):
    return SnowflakeToolkit(session=fake).call_tool(
        "SNOWFLAKE_RUN_SELECT", {"statement": sql}, config
    )


def _fake() -> FakeSnowflake:
    fake = FakeSnowflake()
    fake.add("SELECT 1 AS X", Canned([col("X", "fixed", scale=0)], [[1]]))
    return fake


def test_a_keypair_blob_loads_with_resolved_names_and_the_accounts_host():
    creds = load_credentials(
        blob(role="aisquare_readonly", warehouse='"Mixed Wh"', database="policies")
    )
    assert creds.auth == "keypair"
    assert creds.host == "acme-analytics.snowflakecomputing.com"
    assert creds.role == "AISQUARE_READONLY"
    assert creds.warehouse == "Mixed Wh"
    assert creds.database == "POLICIES" and creds.schema == "PUBLIC"
    assert creds.fingerprint == fingerprint()
    assert creds.token_type == "KEYPAIR_JWT"


@pytest.mark.parametrize(
    ("account", "host", "jwt_account"),
    [
        ("myorg-myaccount", "myorg-myaccount", "MYORG-MYACCOUNT"),
        ("MyOrg-My_Account", "myorg-my-account", "MYORG-MY_ACCOUNT"),
        (
            "myorg-myaccount.privatelink",
            "myorg-myaccount.privatelink",
            "MYORG-MYACCOUNT",
        ),
        ("xy12345", "xy12345", "XY12345"),
        ("xy12345.us-east-2.aws", "xy12345.us-east-2.aws", "XY12345"),
        ("xy12345.us-central1.gcp", "xy12345.us-central1.gcp", "XY12345"),
        (
            "xy12345.west-europe.azure.privatelink",
            "xy12345.west-europe.azure.privatelink",
            "XY12345",
        ),
    ],
)
def test_snowflake_account_identifiers_are_accepted(account, host, jwt_account):
    creds = load_credentials(blob(account=account))
    assert creds.host == f"{host}.snowflakecomputing.com"
    assert creds.base_url == f"https://{host}.snowflakecomputing.com"
    assert creds.jwt_account == jwt_account


@pytest.mark.parametrize(
    "account",
    [
        "https://myorg-myaccount.snowflakecomputing.com",
        "myorg-myaccount.snowflakecomputing.com",
        "evil.example.com/",
        "myorg-myaccount:443",
        "user@myorg-myaccount",
        "myorg-my account",
        "myorg-myaccount.evil.com.x.y",
        "myorg-acct-extra",
        "-myorg",
        "myorg-",
        "127.0.0.1",
        "a",
        "myorg-myaccount/../x",
        "",
        "   ",
        42,
        None,
    ],
)
def test_an_account_that_is_not_an_identifier_is_not_configured(account):
    with pytest.raises(SnowflakeNotConfigured, match="account"):
        load_credentials(
            blob(account=account) if account is not None else blob(account="")
        )


NOT_CONFIGURED = {
    "no_blob": None,
    "not_a_mapping": "account=x",
    "empty": {},
    "no_role": blob(role=""),
    "no_warehouse": blob(warehouse=""),
    "two_part_role": blob(role="A.B"),
    "unknown_auth": blob(auth="password"),
    "no_user": blob(user=""),
    "user_with_dot": blob(user="A.B"),
    "no_key": blob(private_key_pem=""),
    "not_a_key": blob(
        private_key_pem="-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----"
    ),
    "ec_key": blob(private_key_pem=ec_private_key_pem()),
    "short_key": blob(private_key_pem=private_key_pem("short", bits=1024)),
    "encrypted_no_passphrase": blob(
        private_key_pem=private_key_pem("enc", passphrase="pw-1")
    ),
    "encrypted_wrong_passphrase": blob(
        private_key_pem=private_key_pem("enc", passphrase="pw-1"),
        private_key_passphrase="pw-2",
    ),
    "passphrase_not_a_string": blob(private_key_passphrase=123),
    "oauth_no_client_id": oauth_blob(client_id=""),
    "oauth_no_secret": oauth_blob(client_secret=""),
    "oauth_secret_with_space": oauth_blob(client_secret="a b"),
    "oauth_no_token_url": oauth_blob(token_url=""),
    "oauth_http": oauth_blob(token_url="http://idp.example.com/token"),
    "oauth_ip": oauth_blob(token_url="https://10.0.0.1/token"),
    "oauth_metadata_ip": oauth_blob(token_url="https://169.254.169.254/latest"),
    "oauth_localhost": oauth_blob(token_url="https://localhost/token"),
    "oauth_port": oauth_blob(token_url="https://idp.example.com:8443/token"),
    "oauth_userinfo": oauth_blob(token_url="https://u:p@idp.example.com/token"),
    "oauth_fragment": oauth_blob(token_url="https://idp.example.com/token#x"),
    "oauth_scope_not_a_string": oauth_blob(scope=["a"]),
}


@pytest.mark.parametrize(
    "config", list(NOT_CONFIGURED.values()), ids=list(NOT_CONFIGURED)
)
def test_a_missing_or_malformed_blob_is_not_configured(config):
    with pytest.raises(SnowflakeNotConfigured) as raised:
        load_credentials(config)
    text = str(raised.value)
    assert "BEGIN" not in text and CLIENT_SECRET not in text and "pw-" not in text


@pytest.mark.parametrize(
    "config", list(NOT_CONFIGURED.values()), ids=list(NOT_CONFIGURED)
)
def test_every_tool_and_the_source_fail_closed_on_it_before_any_request(config):
    fake = _fake()
    result = _select(fake, config)
    assert not result.successful and result.error_code == "not_connected"
    assert result.error.startswith("Snowflake is not configured")
    with pytest.raises(SnowflakeNotConfigured):
        list(SnowflakeQuerySource(session=fake).pull(config, None))
    assert fake.requests == []


def test_ambient_snowflake_configuration_is_never_consulted(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".snowflake").mkdir(parents=True)
    (home / ".snowflake" / "connections.toml").write_text(
        '[default]\naccount = "ambient-account"\nuser = "AMBIENT"\npassword = "x"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    for name, value in {
        "SNOWFLAKE_ACCOUNT": "ambient-account",
        "SNOWFLAKE_USER": "AMBIENT",
        "SNOWFLAKE_PASSWORD": "ambient-password",
        "SNOWFLAKE_PRIVATE_KEY_PATH": str(tmp_path / "key.p8"),
        "SNOWFLAKE_ROLE": "ACCOUNTADMIN",
        "SNOWFLAKE_WAREHOUSE": "AMBIENT_WH",
        "SNOWFLAKE_DEFAULT_CONNECTION_NAME": "default",
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SnowflakeNotConfigured):
        load_credentials({})
    fake = _fake()
    result = _select(fake, blob())
    assert result.successful, result.error
    (submit,) = fake.submits()
    assert (
        submit["role"] == "AISQUARE_READONLY" and submit["warehouse"] == "AISQUARE_WH"
    )
    assert all(r["url"].startswith("https://acme-analytics.") for r in fake.requests)


def test_the_jwt_carries_snowflakes_claims_and_the_users_key_signs_it():
    creds = load_credentials(blob())
    token = keypair_jwt(creds, now=1_763_373_600)
    header_b64, claims_b64, signature_b64 = token.split(".")
    assert json.loads(_b64(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64(claims_b64))
    assert claims == {
        "iss": f"{ACCOUNT.upper()}.{USER}.{fingerprint()}",
        "sub": f"{ACCOUNT.upper()}.{USER}",
        "iat": 1_763_373_600,
        "exp": 1_763_373_600 + 59 * 60,
    }
    rsa_key().public_key().verify(
        _b64(signature_b64),
        f"{header_b64}.{claims_b64}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_an_encrypted_or_pkcs1_key_with_escaped_newlines_loads_and_works():
    fake = _fake()
    encrypted = private_key_pem("default", passphrase="pass-phrase-1")
    assert _select(
        fake, blob(private_key_pem=encrypted, private_key_passphrase="pass-phrase-1")
    ).successful
    pkcs1 = private_key_pem("default", pkcs1=True).replace("\n", "\\n")
    assert _select(fake, blob(private_key_pem=pkcs1)).successful


def test_a_key_snowflake_does_not_hold_for_the_user_is_unauthorized_and_not_retried():
    fake = _fake()
    result = _select(fake, blob(private_key_pem=private_key_pem("other")))
    assert not result.successful and result.error_code == "unauthorized"
    assert "RSA_PUBLIC_KEY" in result.error and "390144" in result.error
    assert len(fake.api_calls()) == 1  # a fresh JWT from the same key fails the same


def test_the_oauth_token_is_minted_once_per_client_and_reused():
    fake = _fake()
    for _ in range(3):
        result = _select(fake, oauth_blob())
        assert result.successful, result.error
    tokens = [r for r in fake.requests if r["url"] == TOKEN_URL]
    assert len(tokens) == 1
    assert tokens[0]["data"] == {
        "grant_type": "client_credentials",
        "client_id": "client-123",
        "client_secret": CLIENT_SECRET,
        "scope": "api://snowflake/.default",
    }
    assert tokens[0]["allow_redirects"] is False
    submit = next(r for r in fake.api_calls() if r["method"] == "POST")
    assert submit["headers"]["Authorization"] == "Bearer oat-1-abcdef"
    assert submit["headers"]["X-Snowflake-Authorization-Token-Type"] == "OAUTH"


def test_a_cached_oauth_token_snowflake_rejects_is_dropped_and_reminted_once():
    fake = _fake()
    assert _select(fake, oauth_blob()).successful
    fake.revoked.add("oat-1-abcdef")
    result = _select(fake, oauth_blob())
    assert result.successful, result.error
    submits = [r for r in fake.api_calls() if r["method"] == "POST"]
    first, retried = submits[-2], submits[-1]
    assert first["params"]["requestId"] == retried["params"]["requestId"]
    assert retried["params"]["retry"] == "true"
    assert retried["headers"]["Authorization"] == "Bearer oat-2-abcdef"


def test_a_new_oauth_token_snowflake_rejects_is_final():
    fake = _fake()
    fake.revoked.update({"oat-1-abcdef", "oat-2-abcdef"})
    result = _select(fake, oauth_blob())
    assert not result.successful and result.error_code == "unauthorized"
    assert "External OAuth" in result.error
    assert len([r for r in fake.requests if r["url"] == TOKEN_URL]) == 1


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (FakeResponse(401, {"error": "invalid_client"}), "unauthorized"),
        (FakeResponse(400, {"error": "unauthorized_client"}), "unauthorized"),
        (FakeResponse(429, {}, headers={"Retry-After": "7"}), "rate_limited"),
        (FakeResponse(503, {}), "upstream_error"),
        (FakeResponse(200, {"token_type": "Bearer"}), "upstream_error"),
        (FakeResponse(200, content=b"<html>"), "upstream_error"),
    ],
)
def test_token_endpoint_failures_map_to_canonical_codes(answer, code):
    fake = _fake()
    fake.override("token", answer)
    result = _select(fake, oauth_blob())
    assert not result.successful and result.error_code == code, result.error
    assert CLIENT_SECRET not in result.error
    assert fake.api_calls() == []
    if code == "rate_limited":
        assert result.retry_after == 7


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (requests.ConnectTimeout("slow"), "timeout"),
        (requests.ConnectionError("down"), "upstream_error"),
    ],
)
def test_an_unreachable_token_endpoint_is_a_timeout_or_upstream_error(raised, code):
    class Down(FakeSnowflake):
        def request(self, method, url, **kwargs):
            if url == TOKEN_URL:
                raise raised
            return super().request(method, url, **kwargs)

    result = _select(Down(), oauth_blob())
    assert not result.successful and result.error_code == code, result.error


def test_the_client_secret_and_tokens_never_appear_in_errors():
    fake = _fake()
    fake.override(
        "submit",
        FakeResponse(
            500, {"code": "000001", "message": f"boom {CLIENT_SECRET} oat-1-abcdef"}
        ),
    )
    result = _select(fake, oauth_blob())
    assert not result.successful and result.error_code == "upstream_error"
    assert CLIENT_SECRET not in result.error and "oat-1-abcdef" not in result.error
    assert "[redacted]" in result.error


def test_tokens_expire_from_the_cache_before_snowflake_would_refuse_them(monkeypatch):
    fake = _fake()
    assert _select(fake, oauth_blob()).successful
    later = time.time() + 3599 - 200  # within five minutes of expiry
    monkeypatch.setattr(time, "time", lambda: later)
    assert _select(fake, oauth_blob()).successful
    assert len([r for r in fake.requests if r["url"] == TOKEN_URL]) == 2
