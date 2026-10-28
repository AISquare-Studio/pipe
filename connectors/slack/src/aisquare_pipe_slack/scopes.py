"""Scope selection: which OAuth scopes a connection asks Slack for.

Every catalog row declares the scopes it needs per identity, so the scope
lists here are derived, never hand-kept: :data:`USER_SCOPES` /
:data:`BOT_SCOPES` are the unions over the whole catalog (the default — one
consent, no re-consent later, since Slack scopes only ever add up), and the
named groups are the unions over a slice of it:

* by area — ``messages``, ``history``, ``search``, ``conversations``,
  ``dms``, ``reactions``, ``pins``, ``users``, ``workspace``;
* by risk tier — ``read``, ``send``, ``write``, ``destructive`` (so a host
  that enables only read + send can ask for only what those tools need).

A selection mixes group names and raw Slack scopes (``"chat:write"``).
Anything unknown is refused rather than sent to Slack, where it would fail
the whole consent screen.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from aisquare.pipe import OAuthError

from aisquare_pipe_slack.constants import IDENTITY_BOT, IDENTITY_USER, TIERS
from aisquare_pipe_slack.tools import CATALOG, SlackTool

#: ``build_authorize_url``'s identity: one of the two, or both in one consent.
OAuthIdentity = Literal["user", "bot", "both"]
IDENTITY_BOTH: OAuthIdentity = "both"


def _union(tools: Iterable[SlackTool], identity: str) -> tuple[str, ...]:
    scopes: set[str] = set()
    for tool in tools:
        if identity == IDENTITY_USER:
            scopes.update(tool.user_scopes)
        elif tool.bot_scopes is not None:
            scopes.update(tool.bot_scopes)
    return tuple(sorted(scopes))


#: Every user scope the catalog needs (the default for the user identity).
USER_SCOPES: tuple[str, ...] = _union(CATALOG, IDENTITY_USER)
#: Every bot scope the catalog needs (the default for the bot identity).
BOT_SCOPES: tuple[str, ...] = _union(CATALOG, IDENTITY_BOT)

AREAS: tuple[str, ...] = tuple(dict.fromkeys(tool.area for tool in CATALOG))


def _groups() -> dict[str, dict[str, tuple[str, ...]]]:
    groups: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, members in [
        *((area, [t for t in CATALOG if t.area == area]) for area in AREAS),
        *((tier, [t for t in CATALOG if t.tier == tier]) for tier in TIERS),
    ]:
        groups[name] = {
            IDENTITY_USER: _union(members, IDENTITY_USER),
            IDENTITY_BOT: _union(members, IDENTITY_BOT),
        }
    return groups


#: Group name -> {identity -> scopes}.
SCOPE_GROUPS: dict[str, dict[str, tuple[str, ...]]] = _groups()


def resolve_scopes(selection: Iterable[str] | None, identity: str) -> tuple[str, ...]:
    """The Slack scopes for ``selection`` (group names and/or raw scopes;
    None = every scope) and one ``identity`` (``"user"`` or ``"bot"``)."""
    user, bot = scopes_for(selection, "user" if identity == IDENTITY_USER else "bot")
    return user if identity == IDENTITY_USER else bot


def scopes_for(
    selection: Iterable[str] | None, identity: OAuthIdentity
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(user_scopes, bot_scopes)`` to request for an OAuth ``identity``
    (``"user"``, ``"bot"`` or ``"both"``), sorted. ``selection`` mixes group
    names and raw scopes; None = every scope. A raw scope goes to each side
    that uses it. Raises :class:`OAuthError` (``invalid_scope``) for an
    unknown group or a scope no tool of the requested identities uses, and
    for a selection that comes out empty."""
    if identity not in (IDENTITY_USER, IDENTITY_BOT, IDENTITY_BOTH):
        raise OAuthError(
            f"slack: identity must be 'user', 'bot' or 'both', got {identity!r}",
            provider="slack",
            error="invalid_request",
        )
    sides = [IDENTITY_USER, IDENTITY_BOT] if identity == IDENTITY_BOTH else [identity]
    known = {IDENTITY_USER: USER_SCOPES, IDENTITY_BOT: BOT_SCOPES}
    if selection is None:
        chosen = {side: set(known[side]) for side in sides}
    else:
        items = selection.split(",") if isinstance(selection, str) else list(selection)
        chosen = {side: set() for side in sides}
        unknown: list[str] = []
        for raw in items:
            item = str(raw).strip()
            if not item:
                continue
            if item in SCOPE_GROUPS:
                for side in sides:
                    chosen[side].update(SCOPE_GROUPS[item][side])
            elif any(item in known[side] for side in sides):
                for side in sides:
                    if item in known[side]:
                        chosen[side].add(item)
            else:
                unknown.append(item)
        if unknown:
            raise OAuthError(
                f"slack: unknown scope or scope group for identity {identity!r}: "
                f"{', '.join(repr(u) for u in unknown)} (groups: {', '.join(SCOPE_GROUPS)})",
                provider="slack",
                error="invalid_scope",
            )
    if not any(chosen.values()):
        raise OAuthError(
            f"slack: the scope selection is empty for identity {identity!r}",
            provider="slack",
            error="invalid_scope",
        )
    return (
        tuple(sorted(chosen.get(IDENTITY_USER, ()))),
        tuple(sorted(chosen.get(IDENTITY_BOT, ()))),
    )
