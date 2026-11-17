"""Glean credentials: the tenant's server-side blob, checked before any request.

The blob is one JSON object (see the README for the Secrets Manager name)::

    {"host": "acme-be.glean.com", "token": "<Glean Client API token>",
     "token_kind": "user" | "global", "act_as": "reader@acme.com",
     "allowed_hosts": ["glean.acme.internal"]}

* ``host`` is a bare hostname. It must match ``<instance>-be.glean.com``,
  unless the blob itself lists it in ``allowed_hosts`` (a customer with its own
  domain in front of Glean). It is never taken from a caller: tool arguments
  and pull params cannot carry it.
* ``token`` is a Glean-issued Client API token.
* ``token_kind`` ``user`` (default): the token reads as the user it was issued
  for, and ``act_as`` must be absent. ``global``: a Super Admin's token that
  must name the user it reads as in ``act_as``, sent as ``X-Glean-ActAs`` on
  every call.

Anything missing or malformed raises :class:`GleanNotConfigured` with a reason
that never contains the token, and no request is made.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: ``<instance>-be.glean.com``: the host form Glean gives every customer.
GLEAN_HOST_PATTERN = re.compile(r"^[a-z0-9-]+-be\.glean\.com$")
#: A bare DNS hostname (no scheme, port, path, userinfo or trailing dot).
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TOKEN_KINDS = ("user", "global")

#: The blob's keys. They are server-controlled: a caller may set none of them.
SERVER_KEYS = frozenset({"host", "token", "token_kind", "act_as", "allowed_hosts"})


class GleanNotConfigured(ValueError):
    """The Glean credential blob is missing or unusable. The message names
    what is wrong and never includes the token."""


@dataclass(frozen=True)
class GleanCredentials:
    host: str
    token: str = field(repr=False)
    token_kind: str = "user"
    act_as: str | None = None

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/rest/api/v1/"

    def headers(self) -> dict[str, str]:
        """The auth headers for every call: the bearer token, plus
        ``X-Glean-ActAs`` for a global token (always; never for a user token)."""
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.token_kind == "global":
            if not self.act_as:
                raise GleanNotConfigured("a global token needs act_as")
            headers["X-Glean-ActAs"] = self.act_as
        return headers


def _host(value: Any, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GleanNotConfigured("host is missing (expected <instance>-be.glean.com)")
    host = value.strip().lower()
    if not _HOSTNAME.match(host):
        raise GleanNotConfigured(
            "host must be a bare hostname such as acme-be.glean.com "
            "(no scheme, port, path or trailing dot)"
        )
    if GLEAN_HOST_PATTERN.match(host) or host in allowed:
        return host
    raise GleanNotConfigured(
        f"host {host!r} is not a Glean host (<instance>-be.glean.com) and is not in "
        "the credential's allowed_hosts"
    )


def _allowed_hosts(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(h, str) for h in value):
        raise GleanNotConfigured("allowed_hosts must be a list of hostnames")
    hosts = frozenset(h.strip().lower() for h in value)
    bad = sorted(h for h in hosts if not _HOSTNAME.match(h))
    if bad:
        raise GleanNotConfigured(
            f"allowed_hosts holds a value that is not a bare hostname: {bad[0]!r}"
        )
    return hosts


def load_credentials(config: Any) -> GleanCredentials:
    """The credentials in ``config`` (the tenant's blob), or
    :class:`GleanNotConfigured`. Pure: no network."""
    if not isinstance(config, Mapping):
        raise GleanNotConfigured("no Glean credentials are configured")
    token = config.get("token")
    if not isinstance(token, str) or not token.strip():
        raise GleanNotConfigured("token is missing")
    token = token.strip()
    if any(ch.isspace() for ch in token) or not token.isprintable():
        raise GleanNotConfigured("token holds whitespace or control characters")
    kind = config.get("token_kind", "user")
    if kind not in TOKEN_KINDS:
        raise GleanNotConfigured("token_kind must be 'user' or 'global'")
    act_as = config.get("act_as")
    if kind == "global":
        if not isinstance(act_as, str) or not _EMAIL.match(act_as.strip()):
            raise GleanNotConfigured(
                "a global token needs act_as: the email of the user Glean reads as"
            )
        act_as = act_as.strip()
    elif act_as not in (None, ""):
        raise GleanNotConfigured(
            "act_as is only for a global token (token_kind 'global')"
        )
    else:
        act_as = None
    host = _host(config.get("host"), _allowed_hosts(config.get("allowed_hosts")))
    return GleanCredentials(host=host, token=token, token_kind=kind, act_as=act_as)
