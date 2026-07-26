"""Slack connector for aisquare.pipe.

Outbound-only in v1: :class:`SlackMessageSink` posts a message to a channel,
a thread, or as an ephemeral message visible to one user.

Domain-pure by design: the connector receives a ``bot_token`` in ``config``
and targeting in ``params`` — workspace scoping, credential storage,
approval queues, caps/budgets, and the Events API HTTP route all belong to
the host. :mod:`aisquare_pipe_slack.webhook` ships the pure crypto helpers
for hosts that verify inbound Slack requests (v0 signing scheme).

Slack wrinkle worth knowing: the Web API reports most failures as HTTP 200
with ``{"ok": false, "error": "..."}`` — the client normalizes those onto
the same error hierarchy as transport-level failures.
"""

from aisquare_pipe_slack.connector import SlackMessageSink

__all__ = ["SlackMessageSink"]
