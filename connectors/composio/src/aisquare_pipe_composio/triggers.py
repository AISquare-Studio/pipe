"""Composio trigger-event polling source.

Polls Composio's trigger event log (new email, new Slack message, new sheet
row, ...) and yields one ``application/json`` envelope per event — the same
polling shape as the n8n connector. Prerequisite: trigger instances must
already be enabled in Composio (dashboard or API); this source only reads
the events they produce.

Incremental position is a timestamp watermark plus a bounded ring of seen
event ids (the event log is time-windowed rather than strictly cursored
across polls), persisted atomically to a JSON cursor file. When one poll
cycle hits its page cap before draining a window, the page cursor is saved
and the watermark held back, so the next cycle resumes mid-window — events
are delivered at least once, never silently skipped.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator, Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams, Resource

from aisquare_pipe_composio.client import (
    ComposioClient,
    TriggerCursor,
    _epoch_ms,
    load_trigger_cursor,
    save_trigger_cursor,
)
from aisquare_pipe_composio.connector import _resolve_user, _validate_config
from aisquare_pipe_composio.constants import (
    DEFAULT_CURSOR_PATH,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TRIGGER_PAGE_LIMIT,
    JSON_CONTENT_TYPE,
    MAX_TRIGGER_PAGES_PER_POLL,
)

logger = logging.getLogger("aisquare.pipe.composio")

IDEMPOTENCY_PREFIX = "composio:event"


class ComposioTriggersSource(SourceConnector):
    """Polls Composio trigger events and yields one envelope per event."""

    name = "composio-triggers-source"
    version = "0.1.0"
    output_types = [JSON_CONTENT_TYPE]
    auth_type = AuthType.API_KEY
    description = (
        "Polls Composio trigger events (new email, new row, ...) and yields "
        "one envelope per event"
    )
    docs_url = "https://docs.composio.dev/docs/triggers"

    CONFIG_SPEC: dict[str, MetaField] = {
        "api_key": MetaField(type=str, required=True, description="Composio API key"),
        "user_id": MetaField(
            type=str,
            required=False,
            default="default",
            description="Only events for this Composio entity",
        ),
        "trigger_slugs": MetaField(
            type=list,
            required=False,
            description="Filter by trigger type slug, e.g. GMAIL_NEW_GMAIL_MESSAGE",
        ),
        "trigger_ids": MetaField(
            type=list,
            required=False,
            description="Filter by trigger instance id",
        ),
        "toolkit_filter": MetaField(
            type=list, required=False, description="Allow-list of toolkit slugs"
        ),
        "poll_interval_seconds": MetaField(
            type=int,
            required=False,
            default=DEFAULT_POLL_INTERVAL,
            description="How often to poll for new events",
        ),
        "cursor_path": MetaField(
            type=str,
            required=False,
            default=DEFAULT_CURSOR_PATH,
            description="File path for the polling watermark",
        ),
    }

    metadata_spec = {
        "composio_event_id": MetaField(
            type=str, required=True, description="Trigger event id"
        ),
        "composio_trigger_slug": MetaField(
            type=str, required=True, description="Trigger type slug"
        ),
        "composio_trigger_id": MetaField(
            type=str, required=False, description="Trigger instance id"
        ),
        "composio_toolkit": MetaField(
            type=str, required=False, description="Toolkit the event came from"
        ),
        "composio_user_id": MetaField(
            type=str, required=False, description="Composio entity the event belongs to"
        ),
        "composio_connected_account_id": MetaField(
            type=str, required=False, description="Connected account that produced the event"
        ),
        "event_timestamp": MetaField(
            type=str, required=False, description="ISO timestamp of the event"
        ),
        "idempotency_key": MetaField(
            type=str,
            required=True,
            description="composio:event:<id> — stable across re-polls for sink dedup",
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Poll the trigger event log indefinitely and yield event envelopes.

        Supported PullParams keys (for tests/operations):
            max_polls (int): cap the number of poll iterations (default: unlimited)
            sleep (callable): override for time.sleep (default: time.sleep)
            since (str | int): initial watermark when no cursor file exists —
                ISO timestamp or epoch ms (default: now, i.e. only new events)
            limit (int): event-log page size (default 100)
        """
        if params is None:
            params = PullParams()

        client = ComposioClient(config)
        user_id = _resolve_user(config, None)
        poll_interval = int(
            config.get(
                "poll_interval_seconds",
                self.CONFIG_SPEC["poll_interval_seconds"].default,
            )
        )
        cursor_path = config.get("cursor_path", self.CONFIG_SPEC["cursor_path"].default)

        max_polls: int | None = params.get("max_polls", None)
        sleep = params.get("sleep", time.sleep)
        limit = int(params.get("limit", DEFAULT_TRIGGER_PAGE_LIMIT))
        since_ms = _epoch_ms(params.get("since"))

        polls = 0
        while max_polls is None or polls < max_polls:
            state = load_trigger_cursor(cursor_path)
            if state.last_ts_ms == 0 and not state.seen_ids:
                # First run: start at `since`, or now (only future events).
                state.last_ts_ms = (
                    since_ms if since_ms is not None else int(time.time() * 1000)
                )

            try:
                changed = yield from self._poll_once(
                    client, config, user_id, state, limit
                )
            except Exception:
                logger.exception("composio: trigger poll cycle failed")
                changed = False
                if state.pending_cursor is not None:
                    # A stale resume cursor could fail every cycle forever —
                    # drop it so the next cycle rescans the window from the
                    # held watermark (the seen-id ring absorbs the replays).
                    state.pending_cursor = None
                    state.pending_max_ts = 0
                    changed = True

            if changed:
                save_trigger_cursor(cursor_path, state)

            polls += 1
            if max_polls is None or polls < max_polls:
                sleep(poll_interval)

    def _poll_once(
        self,
        client: ComposioClient,
        config: dict,
        user_id: str,
        state: TriggerCursor,
        limit: int,
    ) -> Generator[DataEnvelope, None, bool]:
        """One poll cycle: page through new events, yield unseen ones, and
        advance the watermark in ``state``. Returns whether state changed."""
        trigger_slugs = {
            str(s).upper() for s in (config.get("trigger_slugs") or [])
        }
        trigger_ids = {str(t) for t in (config.get("trigger_ids") or [])}
        toolkit_filter = {
            str(t).lower() for t in (config.get("toolkit_filter") or [])
        }

        changed = False
        cursor = state.pending_cursor
        max_ts = max(state.last_ts_ms, state.pending_max_ts)
        exhausted = False
        for _ in range(MAX_TRIGGER_PAGES_PER_POLL):
            events, next_cursor = client.list_trigger_events(
                user_id=user_id,
                from_ms=state.last_ts_ms or None,
                cursor=cursor,
                limit=limit,
            )

            for event in events:
                event_id = str(event.get("id") or "")
                if not event_id or event_id in state.seen_ids:
                    continue

                state.seen_ids.append(event_id)
                ts_ms = event.get("timestamp_ms")
                if isinstance(ts_ms, int) and ts_ms > max_ts:
                    max_ts = ts_ms
                changed = True

                slug = str(event.get("trigger_slug") or "").upper()
                if trigger_slugs and slug not in trigger_slugs:
                    continue
                if trigger_ids and str(event.get("trigger_id") or "") not in trigger_ids:
                    continue
                toolkit = str(event.get("toolkit") or "").lower()
                if toolkit_filter and toolkit not in toolkit_filter:
                    continue

                yield self._envelope(event, event_id)

            if not next_cursor or not events:
                exhausted = True
                break
            cursor = next_cursor

        if exhausted:
            # Window fully drained — now (and only now) advance the watermark.
            if state.pending_cursor is not None or state.pending_max_ts:
                state.pending_cursor = None
                state.pending_max_ts = 0
                changed = True
            if max_ts > state.last_ts_ms:
                state.last_ts_ms = max_ts
                changed = True
        else:
            # Page cap hit with more pages remaining: hold the watermark and
            # save the page cursor so the next cycle resumes mid-window
            # instead of skipping (or re-reading) the remainder.
            if state.pending_cursor != cursor or state.pending_max_ts != max_ts:
                changed = True
            state.pending_cursor = cursor
            state.pending_max_ts = max_ts
            logger.warning(
                "composio: trigger event window exceeded %d pages; watermark "
                "held — resuming from the saved page cursor next poll",
                MAX_TRIGGER_PAGES_PER_POLL,
            )

        return changed

    def _envelope(self, event: dict[str, Any], event_id: str) -> DataEnvelope:
        metadata: dict[str, Any] = {
            "composio_event_id": event_id,
            "composio_trigger_slug": str(event.get("trigger_slug") or ""),
            "idempotency_key": f"{IDEMPOTENCY_PREFIX}:{event_id}",
        }
        for meta_key, event_key in (
            ("composio_trigger_id", "trigger_id"),
            ("composio_toolkit", "toolkit"),
            ("composio_user_id", "user_id"),
            ("composio_connected_account_id", "connected_account_id"),
            ("event_timestamp", "timestamp"),
        ):
            value = event.get(event_key)
            if value:
                metadata[meta_key] = str(value)

        return DataEnvelope(
            content_type=JSON_CONTENT_TYPE,
            data=event,
            source_id=self.name,
            metadata=metadata,
        )

    def validate_config(self, config: dict) -> bool:
        return _validate_config(config)

    def list_resources(self, config: dict) -> list[Resource]:
        """Browse trigger types (what can be enabled) and the project's
        active trigger instances (what to put in config['trigger_ids'])."""
        client = ComposioClient(config)
        toolkits = [str(t) for t in (config.get("toolkit_filter") or [])] or None

        resources: list[Resource] = []
        for trigger_type in client.list_trigger_types(toolkits=toolkits):
            slug = str(trigger_type.get("slug") or "")
            if not slug:
                continue
            toolkit = trigger_type.get("toolkit") or {}
            resources.append(
                Resource(
                    id=slug,
                    name=str(trigger_type.get("name") or slug),
                    resource_type="trigger_type",
                    metadata={
                        "toolkit": (
                            toolkit.get("slug") if isinstance(toolkit, dict) else toolkit
                        ),
                        "description": trigger_type.get("description", ""),
                    },
                )
            )

        try:
            instances = client.list_trigger_instances()
        except Exception:
            logger.exception("composio: listing trigger instances failed")
            instances = []
        for instance in instances:
            instance_id = str(instance.get("id") or "")
            if not instance_id:
                continue
            slug = str(
                instance.get("trigger_name") or instance.get("trigger_slug") or ""
            )
            resources.append(
                Resource(
                    id=instance_id,
                    name=slug or instance_id,
                    resource_type="trigger_instance",
                    metadata={
                        "trigger_slug": slug,
                        "connected_account_id": instance.get("connected_account_id"),
                        "disabled": instance.get("disabled_at") is not None,
                    },
                )
            )
        return resources

    def supports_streaming(self) -> bool:
        return False
