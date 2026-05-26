"""Auth resolution for the DocuSign connector.

Supports two flows, dispatched by which keys are present in config:
  - JWT Grant flow:           integration_key, user_id, private_key, auth_server
  - Authorization Code flow:  client_id, client_secret, refresh_token, auth_server
"""

from __future__ import annotations

from typing import Any

import requests
from docusign_esign import ApiClient

from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_docusign.constants import (
    DOCUSIGN_SCOPES,
    JWT_EXPIRES_IN,
    OAUTH_TOKEN_TIMEOUT,
)

JWT_KEYS = ("integration_key", "user_id", "private_key", "auth_server")
AUTH_CODE_KEYS = ("client_id", "client_secret", "refresh_token", "auth_server")


def has_valid_auth_keys(config: dict[str, Any]) -> bool:
    """Check whether config has a complete set of credentials for either flow."""
    if all(k in config for k in JWT_KEYS):
        return True
    if all(k in config for k in AUTH_CODE_KEYS):
        return True
    return False


def _coerce_private_key(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ConfigValidationError("private_key must be str or bytes")


def _exchange_refresh_token(config: dict[str, Any]) -> str:
    """POST to /oauth/token with grant_type=refresh_token. Returns access token."""
    auth_server = config["auth_server"].rstrip("/")
    response = requests.post(
        f"https://{auth_server}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": config["refresh_token"],
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
        },
        timeout=OAUTH_TOKEN_TIMEOUT,
    )
    if response.status_code != 200:
        raise ConfigValidationError(
            f"DocuSign OAuth refresh failed ({response.status_code}): {response.text}"
        )
    return response.json()["access_token"]


def _discover_account(
    api_client: ApiClient, access_token: str
) -> tuple[str, str]:
    """Call /oauth/userinfo to find the default account_id + base_uri."""
    user_info = api_client.get_user_info(access_token)
    accounts = user_info.accounts or []
    default = next((a for a in accounts if a.is_default), None) or (accounts[0] if accounts else None)
    if default is None:
        raise ConfigValidationError("No DocuSign accounts available for this user")
    return default.account_id, default.base_uri


def build_client(config: dict[str, Any]) -> tuple[ApiClient, str]:
    """Resolve config → (authenticated ApiClient, account_id)."""
    api_client = ApiClient()

    if all(k in config for k in JWT_KEYS):
        api_client.set_oauth_host_name(config["auth_server"])
        oauth_token = api_client.request_jwt_user_token(
            client_id=config["integration_key"],
            user_id=config["user_id"],
            oauth_host_name=config["auth_server"],
            private_key_bytes=_coerce_private_key(config["private_key"]),
            expires_in=JWT_EXPIRES_IN,
            scopes=DOCUSIGN_SCOPES,
        )
        access_token = oauth_token.access_token
    elif all(k in config for k in AUTH_CODE_KEYS):
        api_client.set_oauth_host_name(config["auth_server"])
        access_token = _exchange_refresh_token(config)
    else:
        raise ConfigValidationError(
            "Missing DocuSign credentials: provide either "
            f"{JWT_KEYS} or {AUTH_CODE_KEYS}"
        )

    api_client.default_headers["Authorization"] = f"Bearer {access_token}"

    account_id = config.get("account_id")
    if not account_id:
        account_id, base_uri = _discover_account(api_client, access_token)
        api_client.host = f"{base_uri}/restapi"
    elif "base_uri" in config:
        api_client.host = f"{config['base_uri']}/restapi"
    else:
        _, base_uri = _discover_account(api_client, access_token)
        api_client.host = f"{base_uri}/restapi"

    return api_client, account_id
