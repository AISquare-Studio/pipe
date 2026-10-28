"""aisquare-pipe-slack — Slack toolkit for aisquare.pipe, acting as a user or as the app's bot."""

from aisquare_pipe_slack.config import (
    SlackConfigError,
    SlackSettings,
    settings_from_config,
)
from aisquare_pipe_slack.constants import (
    DEFAULT_ATTRIBUTION_NAME,
    DEFAULT_TIERS,
    IDENTITY_BOT,
    IDENTITY_USER,
    TIER_DESTRUCTIVE,
    TIER_READ,
    TIER_SEND,
    TIER_WRITE,
    TIERS,
)
from aisquare_pipe_slack.oauth import SLACK_OAUTH, SlackInstallation
from aisquare_pipe_slack.rest import SlackAPIError, SlackWebClient
from aisquare_pipe_slack.scopes import BOT_SCOPES, SCOPE_GROUPS, USER_SCOPES, scopes_for
from aisquare_pipe_slack.toolkit import SlackToolkit

__all__ = [
    "BOT_SCOPES",
    "DEFAULT_ATTRIBUTION_NAME",
    "DEFAULT_TIERS",
    "IDENTITY_BOT",
    "IDENTITY_USER",
    "SCOPE_GROUPS",
    "SLACK_OAUTH",
    "TIERS",
    "TIER_DESTRUCTIVE",
    "TIER_READ",
    "TIER_SEND",
    "TIER_WRITE",
    "USER_SCOPES",
    "SlackAPIError",
    "SlackConfigError",
    "SlackInstallation",
    "SlackSettings",
    "SlackToolkit",
    "SlackWebClient",
    "scopes_for",
    "settings_from_config",
]
