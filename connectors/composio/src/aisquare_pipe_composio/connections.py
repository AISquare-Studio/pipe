"""Connected-account lifecycle helpers.

Tool execution requires the end user (``config['user_id']``) to have an
ACTIVE connected account for the tool's toolkit. These helpers run that
flow programmatically instead of forcing users into the Composio dashboard:

    request = initiate_connection(config, "gmail")
    print("Authorize at:", request.redirect_url)
    account = wait_for_active(config, request.id)

Auth configs are still created once per toolkit in the Composio dashboard
(or via their API); ``initiate_connection`` resolves the toolkit's existing
auth config automatically when there is exactly one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_composio.client import ComposioClient
from aisquare_pipe_composio.connector import _resolve_user

NOT_CONNECTED = "NOT_CONNECTED"


@dataclass
class ConnectionRequest:
    """An in-flight connection request awaiting end-user authorization."""

    id: str
    redirect_url: str | None
    status: str


def initiate_connection(
    config: dict[str, Any],
    toolkit: str,
    *,
    auth_config_id: str | None = None,
    callback_url: str | None = None,
) -> ConnectionRequest:
    """Start a connection for ``config['user_id']`` to a toolkit.

    With ``auth_config_id`` the connection is initiated against that auth
    config; otherwise the toolkit's single existing auth config is used
    (zero or multiple matches raise ConfigValidationError), falling back to
    Composio's default managed auth for the toolkit.
    """
    client = ComposioClient(config)
    user_id = _resolve_user(config, None)

    if auth_config_id:
        raw = client.initiate_connection(
            user_id=user_id,
            auth_config_id=auth_config_id,
            callback_url=callback_url,
        )
    else:
        configs = client.list_auth_configs(toolkit=toolkit)
        if len(configs) > 1:
            ids = [str(c.get("id")) for c in configs]
            raise ConfigValidationError(
                f"Toolkit '{toolkit}' has {len(configs)} auth configs "
                f"({', '.join(ids)}); pass auth_config_id to pick one"
            )
        if len(configs) == 1 and configs[0].get("id"):
            raw = client.initiate_connection(
                user_id=user_id,
                auth_config_id=str(configs[0]["id"]),
                callback_url=callback_url,
            )
        else:
            # No auth config yet — let Composio create/use its managed default.
            raw = client.authorize_toolkit(user_id=user_id, toolkit=toolkit)

    if not raw.get("id"):
        raise ConfigValidationError(
            f"Composio did not return a connection request id for toolkit '{toolkit}'"
        )
    return ConnectionRequest(
        id=str(raw["id"]),
        redirect_url=raw.get("redirect_url"),
        status=str(raw.get("status") or "INITIATED"),
    )


def wait_for_active(
    config: dict[str, Any], connection_id: str, *, timeout: float = 300.0
) -> dict[str, Any]:
    """Block until the connection becomes ACTIVE (or the SDK times out)."""
    return ComposioClient(config).wait_for_connection(connection_id, timeout=timeout)


def list_connections(
    config: dict[str, Any], *, toolkit: str | None = None
) -> list[dict[str, Any]]:
    """Connected accounts for ``config['user_id']``, optionally per toolkit."""
    return ComposioClient(config).list_connected_accounts(
        user_id=_resolve_user(config, None),
        toolkits=[toolkit] if toolkit else None,
    )


def connection_status(config: dict[str, Any], toolkit: str) -> str:
    """The user's connection status for a toolkit.

    Returns a Composio status ("ACTIVE", "INITIATED", "FAILED", "EXPIRED",
    ...) or "NOT_CONNECTED" when no account exists. When several accounts
    exist, an ACTIVE one wins.
    """
    accounts = list_connections(config, toolkit=toolkit)
    if not accounts:
        return NOT_CONNECTED
    statuses = [str(a.get("status") or "").upper() for a in accounts]
    return "ACTIVE" if "ACTIVE" in statuses else (statuses[0] or NOT_CONNECTED)
