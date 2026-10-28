"""SlackMessageSink — a pipeline sink that posts to Slack.

It takes the same ``config`` as :class:`~aisquare_pipe_slack.toolkit.SlackToolkit`
(``access_token`` of either kind, ``identity``, ``attribution_name``,
``bot_attribution``, ``enabled_tiers`` / ``enabled_tools``,
``timeout_seconds``), so one stored Slack connection serves both. What it
posts as the user carries the same visible "Sent by <name>" line the tools
add; as the bot, only with ``bot_attribution``.

Routing, by the envelope:

* a message — ``str`` data as ``text/plain`` (Slack mrkdwn) or
  ``text/markdown`` (standard markdown), or ``dict`` data as
  ``application/json`` with ``text``, ``markdown_text`` and/or ``blocks`` —
  goes to ``chat.postMessage``, or to ``chat.postEphemeral`` with
  ``params["ephemeral_user"]``;
* anything else — ``bytes``, a ``stream``, ``str`` of another type (JSON
  text, CSV...), a ``dict`` of another type — is uploaded as a file with
  Slack's v2 upload, into the channel (or thread).

``params`` are checked before any request: strings must be non-empty
strings and flags booleans (a float ``thread_ts`` would lose digits).

``PushResult.ref`` is the new message's ``ts`` (``message_ts`` for an
ephemeral message) or the uploaded file's ID. A failed push never raises: it
returns ``success=False`` with an error that names no secret, and
``metadata["error_code"]`` (one of core's ``ERROR_*``, as the tools report).
"""

from __future__ import annotations

import json
import logging
import math
import mimetypes
import time
from collections.abc import Callable, Mapping
from typing import Any

import requests
from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_UPSTREAM_ERROR,
    AuthType,
    DataEnvelope,
    MetaField,
    PushParams,
    PushResult,
    SinkConnector,
    ToolError,
    time_budget,
)

from aisquare_pipe_slack.config import (
    SlackConfigError,
    SlackSettings,
    settings_from_config,
)
from aisquare_pipe_slack.constants import (
    CONFIG_ENABLED_TIERS,
    CONFIG_ENABLED_TOOLS,
    TIER_SEND,
)
from aisquare_pipe_slack.files import upload_file
from aisquare_pipe_slack.rest import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    SlackAPIError,
    SlackWebClient,
)
from aisquare_pipe_slack.toolkit import describe_error, error_code
from aisquare_pipe_slack.tools import (
    SlackCall,
    compose_message,
    mrkdwn_marker,
    resolve_channel,
)

logger = logging.getLogger("aisquare.pipe.slack")

#: Content types whose ``str`` data is posted as a message; a ``dict`` is a
#: message only as :data:`JSON_TYPE`.
TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/x-markdown"})
MARKDOWN_TYPES = frozenset({"text/markdown", "text/x-markdown"})
JSON_TYPE = "application/json"
#: ``params`` the sink reads: non-empty strings, and booleans.
STRING_PARAMS = ("channel", "thread_ts", "ephemeral_user", "filename", "title", "initial_comment")
BOOL_PARAMS = ("reply_broadcast", "unfurl_links", "unfurl_media")
#: Largest file the sink uploads (the same cap as downloads).
UPLOAD_MAX_BYTES = 100 * 1024 * 1024
#: The tools whose enablement a push follows: posting is ``send`` tier by
#: default, and enabling one of these slugs alone enables that kind of push.
SEND_SLUG = "SLACK_SEND_MESSAGE"
EPHEMERAL_SLUG = "SLACK_SEND_EPHEMERAL_MESSAGE"
UPLOAD_SLUG = "SLACK_UPLOAD_OR_CREATE_A_FILE_IN_SLACK"


class _PushError(Exception):
    """A push that fails before or without Slack: message plus ERROR_* code."""

    def __init__(self, message: str, code: str = ERROR_INVALID_ARGUMENTS) -> None:
        super().__init__(message)
        self.code = code


class SlackMessageSink(SinkConnector):
    """Post an envelope to a Slack channel, DM or thread — as a message, an
    ephemeral message, or a file.

    ``params``: ``channel`` (required: an ID, or a channel name), and
    optionally ``thread_ts`` (reply in a thread), ``reply_broadcast``,
    ``ephemeral_user`` (only that member sees it; messages only),
    ``unfurl_links`` / ``unfurl_media``, and for files ``filename``,
    ``title``, ``initial_comment``. ``envelope.metadata`` may carry
    ``blocks`` (override the envelope's), ``filename`` and ``title``.

    ``session`` (tests, or a host that manages pooling) is the
    ``requests.Session`` every push goes through instead of a fresh one.
    """

    name = "slack-message-sink"
    version = "0.1.0"
    input_types = ["*/*"]  # noqa: RUF012 — framework contract: class-level spec, never mutated
    auth_type = AuthType.OAUTH2
    description = (
        "Post messages, thread replies, ephemeral messages and files to Slack, as the connected "
        "user (with a 'Sent by' line) or as the app's bot"
    )
    docs_url = "https://docs.slack.dev/reference/methods/chat.postMessage"
    metadata_spec = {  # noqa: RUF012 — framework contract: class-level spec, never mutated
        "blocks": MetaField(type=list, description="Block Kit blocks; override the envelope's own"),
        "filename": MetaField(type=str, description="File name when the envelope is uploaded as a file"),
        "title": MetaField(type=str, description="File title when the envelope is uploaded as a file"),
    }

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep

    def validate_config(self, config: dict) -> bool:
        """Offline: a usable ``access_token`` (non-blank, no inner
        whitespace) and toolkit options the sink can read."""
        if not isinstance(config, Mapping) or not _usable_token(config.get("access_token")):
            return False
        try:
            settings_from_config(config)
        except SlackConfigError:
            return False
        return True

    def max_size(self) -> int | None:
        return UPLOAD_MAX_BYTES

    def push(self, envelope: DataEnvelope, config: dict, params: PushParams | None = None) -> PushResult:
        settings: SlackSettings | None = None
        try:
            config = config if isinstance(config, Mapping) else {}
            options = _options(params)
            channel = options["channel"]
            if not _usable_token(config.get("access_token")):
                raise _PushError("config['access_token'] is missing or not a usable Slack token", ERROR_FORBIDDEN)
            try:
                settings = settings_from_config(config)
            except SlackConfigError as e:
                # forbidden, as in the toolkit: the host's config, not Slack.
                raise _PushError(
                    f"invalid Slack config, nothing is sent until the host fixes it: {e}", ERROR_FORBIDDEN
                ) from None
            token = str(config["access_token"]).strip()
            attribution = settings.attribution(token)
            # Content first: a bad envelope fails before any request. A message
            # is Slack's content fields; a file is its bytes.
            content: dict[str, Any] | bytes = (
                compose_message(_message_content(envelope), attribution, text_arg="text")
                if _is_message(envelope)
                else _file_bytes(envelope)
            )
            is_file = isinstance(content, bytes)
            ephemeral_user = options.get("ephemeral_user")
            if ephemeral_user and is_file:
                raise _PushError("a file can't be sent as an ephemeral message; drop ephemeral_user")
            slug = UPLOAD_SLUG if is_file else EPHEMERAL_SLUG if ephemeral_user else SEND_SLUG
            if not settings.allows(slug, TIER_SEND):
                raise _PushError(
                    f"posting is a 'send' action and is not enabled for this Slack connection: the "
                    f"host enables it by adding 'send' to config[{CONFIG_ENABLED_TIERS!r}] or "
                    f"{slug!r} to config[{CONFIG_ENABLED_TOOLS!r}]",
                    ERROR_FORBIDDEN,
                )
            with self._client(token, config) as client:
                call = SlackCall(client, settings, attribution=attribution)
                channel_id = resolve_channel(call, channel)
                if isinstance(content, bytes):
                    return _push_file(call, channel_id, envelope, content, options)
                return _push_message(call, channel_id, content, options)
        except SlackAPIError as e:
            return _failed(
                describe_error(e, settings),
                error_code(e),
                slack_error=e.error,
                status=e.status,
                retry_after=float(math.ceil(e.retry_after)) if e.retry_after is not None else None,
            )
        except ToolError as e:
            return _failed(str(e), e.code)
        except _PushError as e:
            return _failed(str(e), e.code)
        except Exception as e:  # noqa: BLE001 — a sink reports, never raises
            logger.warning("slack: push failed (%s)", type(e).__name__)
            return _failed(f"slack: push failed ({type(e).__name__})", ERROR_UPSTREAM_ERROR)

    def _client(self, token: str, config: Mapping[str, Any]) -> SlackWebClient:
        return SlackWebClient(
            token,
            session=self._session,
            max_retries=self._max_retries,
            max_retry_wait=self._max_retry_wait,
            budget=time_budget(config),
            sleep=self._sleep,
        )


def _failed(message: str, code: str, **details: Any) -> PushResult:
    metadata: dict[str, Any] = {"error_code": code}
    metadata.update({k: v for k, v in details.items() if v is not None})
    return PushResult(success=False, error=message, metadata=metadata)


def _base_type(content_type: Any) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower()


def _usable_token(token: Any) -> bool:
    """Core's toolkit rule: a non-blank string without inner whitespace."""
    return isinstance(token, str) and bool(token.strip()) and not any(ch.isspace() for ch in token.strip())


def _options(params: PushParams | None) -> dict[str, Any]:
    """The params the sink reads, checked: non-empty strings and booleans,
    ``channel`` required. Anything else is refused before any request."""
    raw = params.params if isinstance(params, PushParams) else None
    raw = raw if isinstance(raw, Mapping) else {}
    options: dict[str, Any] = {}
    for name in STRING_PARAMS:
        value = raw.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise _PushError(f"params[{name!r}] must be a non-empty string")
        options[name] = value.strip()
    for name in BOOL_PARAMS:
        value = raw.get(name)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise _PushError(f"params[{name!r}] must be true or false")
        options[name] = value
    if "channel" not in options:
        raise _PushError("params['channel'] is required (a channel ID or name)")
    return options


def _is_message(envelope: DataEnvelope) -> bool:
    if envelope.stream is not None:
        return False
    kind = _base_type(envelope.content_type)
    if isinstance(envelope.data, str):
        return kind in TEXT_TYPES
    return isinstance(envelope.data, dict) and kind == JSON_TYPE


def _message_content(envelope: DataEnvelope) -> dict[str, Any]:
    """The arguments :func:`compose_message` reads: ``markdown_text``,
    ``text`` and/or ``blocks``."""
    data = envelope.data
    args: dict[str, Any]
    if isinstance(data, str):
        markdown = _base_type(envelope.content_type) in MARKDOWN_TYPES
        args = {"markdown_text": data} if markdown else {"text": data}
    else:
        assert isinstance(data, dict)
        args = {k: data[k] for k in ("markdown_text", "text", "blocks") if data.get(k) is not None}
        if not args and envelope.metadata.get("blocks") is None:
            raise _PushError("a JSON envelope must carry text, markdown_text or blocks")
    blocks = envelope.metadata.get("blocks")
    if blocks is not None:
        args["blocks"] = blocks
    return args


def _file_bytes(envelope: DataEnvelope) -> bytes:
    if envelope.stream is not None:
        content = _read_capped(envelope.stream)
    elif isinstance(envelope.data, (bytes, bytearray)):
        content = bytes(envelope.data)
    elif isinstance(envelope.data, str):
        content = envelope.data.encode("utf-8")
    elif isinstance(envelope.data, dict):
        content = json.dumps(envelope.data, ensure_ascii=False).encode("utf-8")
    else:
        raise _PushError(f"unsupported envelope data: {type(envelope.data).__name__}")
    if not content:
        raise _PushError("the envelope is empty: nothing to upload")
    if len(content) > UPLOAD_MAX_BYTES:
        raise _PushError(f"the file is over the {UPLOAD_MAX_BYTES // (1024 * 1024)} MiB upload cap")
    return content


def _read_capped(stream: Any) -> bytes:
    """Read a stream to its end, failing once it passes the upload cap."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        if not isinstance(chunk, (bytes, bytearray)):
            raise _PushError("the envelope's stream must yield bytes")
        size += len(chunk)
        if size > UPLOAD_MAX_BYTES:
            raise _PushError(f"the file is over the {UPLOAD_MAX_BYTES // (1024 * 1024)} MiB upload cap")
        chunks.append(bytes(chunk))


def _push_message(
    call: SlackCall, channel: str, fields: Mapping[str, Any], options: Mapping[str, Any]
) -> PushResult:
    ephemeral_user = options.get("ephemeral_user")
    if ephemeral_user:
        body = {"channel": channel, "user": ephemeral_user, "thread_ts": options.get("thread_ts")}
        payload = call.call("chat.postEphemeral", {**body, **fields}, json_body=True)
        ref = payload.get("message_ts")
    else:
        names = ("thread_ts", "reply_broadcast", "unfurl_links", "unfurl_media")
        body = {"channel": channel, **{n: options.get(n) for n in names}}
        payload = call.call("chat.postMessage", {**body, **fields}, json_body=True)
        ref = payload.get("ts")
    return PushResult(
        success=True,
        ref=ref if isinstance(ref, str) else None,
        metadata={
            "kind": "message",
            "channel": payload.get("channel") or channel,
            "ephemeral": bool(ephemeral_user),
        },
    )


def _push_file(
    call: SlackCall,
    channel: str,
    envelope: DataEnvelope,
    content: bytes,
    options: Mapping[str, Any],
) -> PushResult:
    filename = _text(options.get("filename")) or _text(envelope.metadata.get("filename")) or _default_name(envelope)
    title = _text(options.get("title")) or _text(envelope.metadata.get("title"))
    comment = _text(options.get("initial_comment"))
    if call.attribution:
        marker = f"_{mrkdwn_marker(call.attribution)}_"
        comment = f"{comment}\n{marker}" if comment else marker
    uploaded = upload_file(
        call.client,
        content,
        filename=filename,
        title=title,
        channels=channel,
        initial_comment=comment,
        thread_ts=_text(options.get("thread_ts")),
    )
    file_id = uploaded.get("id")
    return PushResult(
        success=True,
        ref=file_id if isinstance(file_id, str) else None,
        metadata={
            "kind": "file",
            "channel": channel,
            "file_id": file_id,
            "name": uploaded.get("name") or filename,
            "size": len(content),
            **({"permalink": uploaded["permalink"]} if isinstance(uploaded.get("permalink"), str) else {}),
        },
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _default_name(envelope: DataEnvelope) -> str:
    extension = mimetypes.guess_extension(_base_type(envelope.content_type)) or ""
    return f"file{extension}"


__all__ = ["UPLOAD_MAX_BYTES", "SlackMessageSink"]
