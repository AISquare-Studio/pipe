"""Auth resolution for the Salesforce connector.

Supports two flows, dispatched by which keys are present in config:
  - OAuth2 refresh-token flow: client_id, client_secret, refresh_token, instance_url
  - Username/password flow:    username, password, security_token (+ optional domain)
"""

from __future__ import annotations

from typing import Any

import requests
from simple_salesforce import Salesforce

from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_salesforce.constants import OAUTH_TOKEN_TIMEOUT

OAUTH_KEYS = ("client_id", "client_secret", "refresh_token", "instance_url")
USERPASS_KEYS = ("username", "password", "security_token")


def has_valid_auth_keys(config: dict[str, Any]) -> bool:
    """Check whether config has a complete set of credentials for either flow."""
    if all(k in config for k in OAUTH_KEYS):
        return True
    if all(k in config for k in USERPASS_KEYS):
        return True
    return False


def _refresh_access_token(config: dict[str, Any]) -> tuple[str, str]:
    """Exchange a refresh_token for an access token.

    Returns (instance_url, access_token) — Salesforce's token endpoint
    returns the canonical instance_url for the org, which may differ from
    the one supplied in config.
    """
    instance_url = config["instance_url"].rstrip("/")
    response = requests.post(
        f"{instance_url}/services/oauth2/token",
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
            f"Salesforce OAuth refresh failed ({response.status_code}): {response.text}"
        )
    data = response.json()
    return data["instance_url"], data["access_token"]


def build_client(config: dict[str, Any]) -> Salesforce:
    """Resolve config → an authenticated simple_salesforce.Salesforce instance."""
    if all(k in config for k in OAUTH_KEYS):
        instance_url, access_token = _refresh_access_token(config)
        return Salesforce(instance_url=instance_url, session_id=access_token)
    if all(k in config for k in USERPASS_KEYS):
        return Salesforce(
            username=config["username"],
            password=config["password"],
            security_token=config["security_token"],
            domain=config.get("domain", "login"),
        )
    raise ConfigValidationError(
        "Missing salesforce credentials: provide either "
        f"{OAUTH_KEYS} or {USERPASS_KEYS}"
    )
