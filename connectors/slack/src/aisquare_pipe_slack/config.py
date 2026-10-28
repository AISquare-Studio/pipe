"""The toolkit's non-secret config: identity, enabled tiers and tools, attribution.

A host passes these next to ``access_token`` in the ``config`` of every
``call_tool``. Every key is optional; the defaults are the safe ones (act as
the user, read + send only, "Sent by pipe" on the user's messages). Values
are checked strictly: a typo in a tier name fails the call with a message
naming the key, instead of silently changing what an agent may do.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aisquare_pipe_slack.constants import (
    ATTRIBUTION_TEMPLATE,
    CONFIG_ATTRIBUTION_NAME,
    CONFIG_BOT_ATTRIBUTION,
    CONFIG_ENABLED_TIERS,
    CONFIG_ENABLED_TOOLS,
    CONFIG_IDENTITY,
    DEFAULT_ATTRIBUTION_NAME,
    DEFAULT_TIERS,
    IDENTITIES,
    IDENTITY_BOT,
    IDENTITY_USER,
    MAX_ATTRIBUTION_NAME,
    TIERS,
)


class SlackConfigError(ValueError):
    """A config value the toolkit cannot use. The message names the key."""


@dataclass(frozen=True)
class SlackSettings:
    """What one call may do and how it presents itself."""

    identity: str = IDENTITY_USER
    enabled_tiers: frozenset[str] = frozenset(DEFAULT_TIERS)
    enabled_tools: frozenset[str] = frozenset()
    attribution_name: str = DEFAULT_ATTRIBUTION_NAME
    bot_attribution: bool = False

    @property
    def attribution(self) -> str | None:
        """The visible marker for messages this call posts, or None. Always
        on for the user identity (the message shows under the user's name);
        on for the bot only when ``bot_attribution`` is set."""
        if self.identity == IDENTITY_USER or self.bot_attribution:
            return ATTRIBUTION_TEMPLATE.format(name=self.attribution_name)
        return None

    def allows(self, slug: str, tier: str) -> bool:
        return tier in self.enabled_tiers or slug in self.enabled_tools


def settings_from_config(config: Mapping[str, Any]) -> SlackSettings:
    """Read the toolkit's options from ``config``; raise
    :class:`SlackConfigError` for a value it cannot use."""
    identity = config.get(CONFIG_IDENTITY)
    if identity is None:
        identity = IDENTITY_USER
    if identity not in IDENTITIES:
        raise SlackConfigError(
            f"config[{CONFIG_IDENTITY!r}] must be {' or '.join(repr(i) for i in IDENTITIES)}"
        )
    tiers = _names(config, CONFIG_ENABLED_TIERS)
    if tiers is None:
        tiers = frozenset(DEFAULT_TIERS)
    unknown = sorted(tiers - set(TIERS))
    if unknown:
        raise SlackConfigError(
            f"config[{CONFIG_ENABLED_TIERS!r}] has unknown tier(s) {', '.join(map(repr, unknown))}; "
            f"tiers are {', '.join(TIERS)}"
        )
    tools = _names(config, CONFIG_ENABLED_TOOLS) or frozenset()
    bot_attribution = config.get(CONFIG_BOT_ATTRIBUTION)
    if bot_attribution is None:
        bot_attribution = False
    if not isinstance(bot_attribution, bool):
        raise SlackConfigError(f"config[{CONFIG_BOT_ATTRIBUTION!r}] must be true or false")
    return SlackSettings(
        identity=identity,
        enabled_tiers=tiers,
        enabled_tools=frozenset(slug.upper() for slug in tools),
        attribution_name=_attribution_name(config.get(CONFIG_ATTRIBUTION_NAME)),
        bot_attribution=bot_attribution,
    )


def _names(config: Mapping[str, Any], key: str) -> frozenset[str] | None:
    """A list of names (a single comma-separated string is accepted too), or
    None when the key is absent."""
    value = config.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple, set, frozenset)) or not all(
        isinstance(item, str) for item in value
    ):
        raise SlackConfigError(f"config[{key!r}] must be a list of names")
    return frozenset(item.strip() for item in value if item.strip())


def _attribution_name(value: Any) -> str:
    """The configured name, whitespace collapsed, escaped for Slack's mrkdwn
    (``&``, ``<``, ``>``) and cut to :data:`MAX_ATTRIBUTION_NAME`; the
    default when unset or blank."""
    if value is None:
        return DEFAULT_ATTRIBUTION_NAME
    if not isinstance(value, str):
        raise SlackConfigError(f"config[{CONFIG_ATTRIBUTION_NAME!r}] must be a string")
    name = " ".join(value.split())[:MAX_ATTRIBUTION_NAME].strip()
    if not name:
        return DEFAULT_ATTRIBUTION_NAME
    return name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "IDENTITY_BOT",
    "IDENTITY_USER",
    "SlackConfigError",
    "SlackSettings",
    "settings_from_config",
]
