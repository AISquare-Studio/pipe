"""Slack sink connector for aisquare.pipe."""

from __future__ import annotations

from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PushParams,
    PushResult,
)

from aisquare_pipe_slack.client import SlackClient, SlackError


class SlackMessageSink(SinkConnector):
    """Post a message to a Slack channel, thread, or user (ephemeral).

    The envelope carries the message body — a plain string, or a dict with
    ``text`` and/or Block Kit ``blocks``. ``params`` carry the targeting:
    ``channel`` (required), ``thread_ts`` (reply in thread), and
    ``ephemeral_user`` (deliver via ``chat.postEphemeral`` to that user).
    """

    name = "slack-message-sink"
    version = "0.1.0"
    input_types = ["application/json", "text/plain"]
    auth_type = AuthType.OAUTH2
    description = "Post channel/thread/ephemeral messages to Slack"
    docs_url = "https://api.slack.com/methods/chat.postMessage"
    metadata_spec = {
        "blocks": MetaField(
            type=list, required=False, description="Block Kit blocks (override envelope blocks)"
        ),
        "username": MetaField(
            type=str, required=False, description="Bot display-name override (channel posts)"
        ),
        "icon_emoji": MetaField(
            type=str, required=False, description="Bot icon override, e.g. ':sparkles:'"
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        channel = params.get("channel", "") if params else ""
        if not channel:
            return PushResult(success=False, error="params.channel is required")

        text, blocks = self._message_body(envelope)
        if text is None:
            return PushResult(
                success=False,
                error="envelope.data must be a str or a dict with text/blocks",
            )
        if not text and not blocks:
            return PushResult(success=False, error="envelope carries neither text nor blocks")
        if not config.get("bot_token"):
            return PushResult(success=False, error="config.bot_token is required")

        thread_ts = params.get("thread_ts", "") if params else ""
        ephemeral_user = params.get("ephemeral_user", "") if params else ""

        try:
            client = SlackClient(config)
            if ephemeral_user:
                payload = client.post_ephemeral(
                    channel=channel,
                    user=ephemeral_user,
                    text=text,
                    blocks=blocks,
                    thread_ts=thread_ts,
                )
                ref = payload.get("message_ts", "")
            else:
                payload = client.post_message(
                    channel=channel,
                    text=text,
                    blocks=blocks,
                    thread_ts=thread_ts,
                    username=envelope.metadata.get("username", ""),
                    icon_emoji=envelope.metadata.get("icon_emoji", ""),
                )
                ref = payload.get("ts", "")
        except SlackError as exc:
            return PushResult(success=False, error=str(exc))

        return PushResult(
            success=True,
            ref=ref,
            metadata={
                "channel": payload.get("channel", channel),
                "ephemeral": bool(ephemeral_user),
            },
        )

    def _message_body(
        self, envelope: DataEnvelope
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Normalize envelope.data to ``(text, blocks)``; text=None means unsupported."""
        blocks = envelope.metadata.get("blocks") or []
        if isinstance(envelope.data, str):
            return envelope.data, blocks
        if isinstance(envelope.data, dict):
            text = str(envelope.data.get("text", "") or "")
            blocks = blocks or list(envelope.data.get("blocks") or [])
            return text, blocks
        return None, []

    def validate_config(self, config: dict) -> bool:
        return SlackClient(config).validate()
