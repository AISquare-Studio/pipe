"""Events API helpers: Slack's documented signing vector, the replay window,
header lookup, typed parsing of Slack's own example payloads
(``fixtures/slack_events.json``), malformed bodies, and envelopes."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from requests.structures import CaseInsensitiveDict

from aisquare_pipe_slack import events
from aisquare_pipe_slack.events import (
    AppMention,
    AppRateLimited,
    AppUninstalled,
    EventCallback,
    Message,
    Reaction,
    SlackEvent,
    SlackEventError,
    SlackRequestError,
    SlackSignatureError,
    TokensRevoked,
    UrlVerification,
    event_to_envelope,
    parse_event,
    parse_request,
    timestamp_in_window,
    trigger_slug,
    verify_request,
    verify_slack_signature,
)

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "slack_events.json").read_text())
VECTOR = FIXTURES["signing_vector"]
EVENTS: dict[str, dict[str, Any]] = FIXTURES["events"]
SECRET = VECTOR["signing_secret"]
NOW = 1_800_000_000


def body_of(name: str) -> bytes:
    return json.dumps(EVENTS[name]).encode()


def sign(body: bytes, ts: int | str = NOW, secret: str = SECRET) -> str:
    base = f"v0:{ts}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def headers(body: bytes, ts: int | str = NOW, **extra: str) -> dict[str, str]:
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sign(body, ts), **extra}


def variant(name: str, **event: Any) -> dict[str, Any]:
    """A fixture with fields of its inner event replaced (None drops one)."""
    body = json.loads(json.dumps(EVENTS[name]))
    for key, value in event.items():
        if value is None:
            body["event"].pop(key, None)
        else:
            body["event"][key] = value
    return body


class TestSignature:
    def test_slacks_documented_vector(self):
        assert verify_slack_signature(
            VECTOR["body"].encode(), VECTOR["timestamp"], VECTOR["signature"], VECTOR["signing_secret"]
        )

    def test_the_vector_through_verify_request(self):
        given = {"x-slack-request-timestamp": VECTOR["timestamp"], "x-slack-signature": VECTOR["signature"]}
        assert verify_request(given, VECTOR["body"].encode(), SECRET, now=int(VECTOR["timestamp"]) + 60) is None

    @pytest.mark.parametrize("ts", ["0" + VECTOR["timestamp"], " " + VECTOR["timestamp"], VECTOR["timestamp"] + " "])
    def test_the_timestamp_is_signed_exactly_as_sent(self, ts):
        # Slack's base string uses the header as sent: the same number
        # written differently is a different signature.
        assert not verify_slack_signature(VECTOR["body"].encode(), ts, VECTOR["signature"], SECRET)

    def test_an_int_timestamp_is_its_decimal(self):
        assert verify_slack_signature(
            VECTOR["body"].encode(), int(VECTOR["timestamp"]), VECTOR["signature"], SECRET
        )

    def test_a_str_body_is_its_utf8_bytes(self):
        assert verify_slack_signature(VECTOR["body"], VECTOR["timestamp"], VECTOR["signature"], SECRET)

    def test_tampered_body_fails(self):
        assert not verify_slack_signature(
            VECTOR["body"].encode() + b"&x=1", VECTOR["timestamp"], VECTOR["signature"], SECRET
        )

    def test_reserialized_json_fails(self):
        # Slack signs the raw bytes: parsing and re-dumping changes them.
        body = body_of("app_mention")
        signature = sign(body)
        redumped = json.dumps(json.loads(body), separators=(",", ":")).encode()
        assert verify_slack_signature(body, NOW, signature, SECRET)
        assert not verify_slack_signature(redumped, NOW, signature, SECRET)

    def test_wrong_secret_or_timestamp_fails(self):
        body = body_of("app_mention")
        assert not verify_slack_signature(body, NOW, sign(body, secret="another-secret"), SECRET)
        assert not verify_slack_signature(body, NOW + 1, sign(body), SECRET)

    @pytest.mark.parametrize(
        "args",
        [
            (b"{}", "", "v0=abc", SECRET),
            (b"{}", "1", "", SECRET),
            (b"{}", "1", "v0=abc", ""),
            (None, "1", "v0=abc", SECRET),
            (b"{}", None, "v0=abc", SECRET),
            (b"{}", "1", None, SECRET),
            (b"{}", "1", "v0=abc", None),
        ],
    )
    def test_missing_parts_fail_closed(self, args):
        assert verify_slack_signature(*args) is False

    @pytest.mark.parametrize(
        "signature",
        ["a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503", "v1=" + "0" * 64, "v0=", "v0=éé"],
    )
    def test_malformed_signatures_fail_without_raising(self, signature):
        assert verify_slack_signature(VECTOR["body"].encode(), VECTOR["timestamp"], signature, SECRET) is False

    def test_unencodable_inputs_fail_without_raising(self):
        assert verify_slack_signature("\ud800", "1", "v0=abc", SECRET) is False
        assert verify_slack_signature(b"{}", "1", "v0=abc", "\ud800") is False


class TestWindow:
    @pytest.mark.parametrize("offset, inside", [(0, True), (300, True), (-300, True), (301, False), (-301, False)])
    def test_five_minutes_either_way(self, offset, inside):
        assert timestamp_in_window(str(NOW + offset), now=NOW) is inside

    @pytest.mark.parametrize(
        "ts", ["1800000000.5", "1e9", "-5", "", " ", "abc", "0x10", True, None, 1.5, "１８", f" {NOW}", f"{NOW}\n"]
    )
    def test_only_whole_seconds(self, ts):
        assert timestamp_in_window(ts, now=NOW) is False

    def test_an_int_is_fine(self):
        assert timestamp_in_window(NOW, now=NOW)

    @pytest.mark.parametrize(
        "ts",
        [10**400, -(10**400), 2**1024, 10**5000 - 1, "9" * 400, "9" * 5000, 10**15, "1" * 16],
        ids=["int-400-digits", "negative", "2**1024", "int-5000-digits", "str-400-digits", "str-5000-digits",
             "10**15", "str-16-digits"],
    )
    def test_huge_numbers_are_refused_without_raising(self, ts):
        # A host may pass int(header) itself: never an OverflowError (a
        # float clock minus a huge int) or ValueError (Python's 4300-digit
        # str/int limit), always False.
        assert timestamp_in_window(ts, now=float(NOW)) is False
        assert timestamp_in_window(ts) is False
        assert verify_slack_signature(b"{}", ts, "v0=" + "0" * 64, SECRET) is False

    def test_defaults_to_the_clock(self, monkeypatch):
        monkeypatch.setattr(events.time, "time", lambda: float(NOW))
        assert timestamp_in_window(str(NOW))
        assert not timestamp_in_window(str(NOW - 1000))


class TestVerifyRequest:
    def _reason(self, given: Any, body: bytes, secret: str = SECRET, now: float = NOW) -> str:
        with pytest.raises(SlackSignatureError) as exc:
            verify_request(given, body, secret, now=now)
        return exc.value.reason

    def test_good_request(self):
        body = body_of("app_mention")
        assert verify_request(headers(body), body, SECRET, now=NOW) is None

    @pytest.mark.parametrize(
        "given",
        [
            lambda h: {k.lower(): v for k, v in h.items()},
            lambda h: {k.upper(): v for k, v in h.items()},
            lambda h: CaseInsensitiveDict(h),
            lambda h: [(k.lower().encode(), v.encode()) for k, v in h.items()],  # ASGI
            lambda h: {k: [v] for k, v in h.items()},  # multi-dict
        ],
        ids=["lower", "upper", "requests", "asgi", "multidict"],
    )
    def test_headers_are_case_insensitive_in_any_shape(self, given):
        body = body_of("app_mention")
        assert verify_request(given(headers(body)), body, SECRET, now=NOW) is None

    def test_surrounding_header_whitespace_is_http_framing(self):
        # HTTP strips optional whitespace around a header value; the
        # digits are what Slack signed.
        body = body_of("app_mention")
        given = {**headers(body), "X-Slack-Request-Timestamp": f" {NOW} "}
        assert verify_request(given, body, SECRET, now=NOW) is None

    def test_a_correctly_signed_but_old_request_is_refused(self):
        # The replay check is not optional: the signature alone would pass.
        body = body_of("app_mention")
        old = NOW - 301
        assert verify_slack_signature(body, old, sign(body, old), SECRET)
        assert self._reason(headers(body, old), body) == "stale_timestamp"

    def test_reasons(self):
        body = body_of("app_mention")
        good = headers(body)
        assert self._reason({"X-Slack-Signature": good["X-Slack-Signature"]}, body) == "missing_timestamp"
        assert self._reason({"X-Slack-Request-Timestamp": str(NOW)}, body) == "missing_signature"
        assert self._reason({**good, "X-Slack-Request-Timestamp": f"{NOW}.0"}, body) == "bad_timestamp"
        assert self._reason({**good, "X-Slack-Request-Timestamp": "9" * 5000}, body) == "bad_timestamp"
        assert self._reason({**good, "X-Slack-Request-Timestamp": f"0{NOW}"}, body) == "bad_signature"
        assert self._reason(good, body + b" ") == "bad_signature"
        assert self._reason(good, body, secret="") == "no_signing_secret"
        assert self._reason(good, None) == "bad_body"  # type: ignore[arg-type]

    def test_conflicting_duplicate_headers_fail_closed(self):
        body = body_of("app_mention")
        good = headers(body)
        pairs = [*good.items(), ("x-slack-signature", "v0=" + "0" * 64)]
        assert self._reason(pairs, body) == "missing_signature"
        # The same value twice is still one value.
        assert verify_request([*good.items(), *good.items()], body, SECRET, now=NOW) is None

    def test_errors_are_value_errors_and_carry_no_secret(self):
        body = body_of("app_mention")
        given = headers(body)
        with pytest.raises(ValueError) as exc:
            verify_request(given, body + b"x", SECRET, now=NOW)
        assert isinstance(exc.value, SlackRequestError)
        for secret in (SECRET, given["X-Slack-Signature"], body.decode()):
            assert secret not in str(exc.value)


class TestParse:
    def test_url_verification(self):
        event = parse_event(body_of("url_verification"))
        assert isinstance(event, UrlVerification)
        assert event.response() == {"challenge": EVENTS["url_verification"]["challenge"]}

    def test_app_mention(self):
        event = parse_event(body_of("app_mention"))
        assert isinstance(event, AppMention)
        assert (event.event_id, event.team_id, event.api_app_id) == ("Ev123ABC456", "T123ABC456", "A123ABC456")
        assert (event.channel, event.user, event.ts) == ("C123ABC456", "U123ABC456", "1515449522.000016")
        assert event.text == "What is the hour of the pearl, <@U0LAN0Z89>?"
        assert event.event_type == "app_mention"
        assert event.event_time == 1515449522000016
        assert not event.is_thread_reply

    def test_message_im(self):
        event = parse_event(body_of("message.im"))
        assert isinstance(event, Message)
        assert event.is_direct_message and event.is_new and not event.is_from_bot
        assert (event.channel, event.user, event.text) == ("D024BE91L", "U2147483697", "Hello hello can you hear me?")
        assert event.channel_type == "im" and event.subtype is None

    def test_thread_replies_and_bot_messages(self):
        reply = parse_event(variant("message.im", thread_ts="1355517500.000001"))
        assert isinstance(reply, Message) and reply.is_thread_reply
        own = parse_event(variant("message.im", bot_id="B0BOT", subtype="bot_message", user=None))
        assert isinstance(own, Message) and own.is_from_bot and own.user is None

    @pytest.mark.parametrize("name, added", [("reaction_added", True), ("reaction_removed", False)])
    def test_reactions(self, name, added):
        event = parse_event(body_of(name))
        assert isinstance(event, Reaction)
        assert event.added is added
        assert (event.user, event.item_type, event.item_channel, event.item_user) == (
            "U123ABC456", "message", "C123ABC456", "U222222222",
        )
        assert event.item_ts == EVENTS[name]["event"]["item"]["ts"]

    def test_tokens_revoked_and_app_uninstalled(self):
        revoked = parse_event(body_of("tokens_revoked"))
        assert isinstance(revoked, TokensRevoked)
        assert (revoked.oauth_user_ids, revoked.bot_user_ids) == (("UXXXXXXXX",), ("UXXXXXXXX",))
        gone = parse_event(body_of("app_uninstalled"))
        assert isinstance(gone, AppUninstalled) and gone.team_id == "T123ABC456"

    def test_app_rate_limited(self):
        event = parse_event(body_of("app_rate_limited"))
        assert isinstance(event, AppRateLimited)
        assert (event.team_id, event.minute_rate_limited) == ("T123ABC456", 1518467820)

    def test_an_unknown_event_type_is_a_plain_callback(self):
        body = variant("app_uninstalled", type="team_join", user={"id": "U0NEW"})
        event = parse_event(body)
        assert type(event) is EventCallback
        assert event.event_type == "team_join" and event.event["user"] == {"id": "U0NEW"}

    def test_an_unknown_outer_type_is_acknowledged(self):
        event = parse_event({"type": "something_new", "x": 1})
        assert type(event) is SlackEvent and event.type == "something_new"

    @pytest.mark.parametrize(
        "name, missing", [("message.im", "channel"), ("app_mention", "ts"), ("reaction_added", "item")]
    )
    def test_a_known_type_without_its_fields_falls_back(self, name, missing):
        event = parse_event(variant(name, **{missing: None}))
        assert type(event) is EventCallback

    def test_an_already_decoded_body(self):
        assert isinstance(parse_event(EVENTS["app_mention"]), AppMention)

    def test_retry_headers(self):
        event = parse_event(body_of("app_mention"), {"x-slack-retry-num": "2", "X-Slack-Retry-Reason": "http_timeout"})
        assert (event.retry_num, event.retry_reason, event.is_retry) == (2, "http_timeout", True)
        first = parse_event(body_of("app_mention"), {})
        assert (first.retry_num, first.retry_reason, first.is_retry) == (None, None, False)

    @pytest.mark.parametrize("num, reason", [("two", "http timeout"), ("²", "<script>"), ("1234", "x" * 65)])
    def test_odd_retry_headers_are_dropped(self, num, reason):
        event = parse_event(body_of("app_mention"), {"X-Slack-Retry-Num": num, "X-Slack-Retry-Reason": reason})
        assert (event.retry_num, event.retry_reason) == (None, None)

    def test_the_deprecated_verification_token_is_not_in_the_repr(self):
        event = parse_event(body_of("app_mention"))
        assert "ZZZZZZWSxiZZZ2yIvs3peJ" not in repr(event)


class TestMalformed:
    @pytest.mark.parametrize(
        "body, reason",
        [
            (b"not json", "not_json"),
            (b"\xff\xfe{}", "not_json"),
            (b"[" * 100_000, "not_json"),
            ("payload=%7B%7D", "not_json"),  # a form-encoded interactivity body
            (b"[]", "not_an_object"),
            (b'"text"', "not_an_object"),
            (b"{}", "missing_type"),
            (b'{"type": 5}', "missing_type"),
            (b'{"type": "url_verification"}', "missing_challenge"),
            (b'{"type": "url_verification", "challenge": ""}', "missing_challenge"),
            (b'{"type": "event_callback", "event_id": "Ev1"}', "missing_event"),
            (b'{"type": "event_callback", "event_id": "Ev1", "event": {}}', "missing_event"),
            (b'{"type": "event_callback", "event_id": "Ev1", "event": ["x"]}', "missing_event"),
            (b'{"type": "event_callback", "event": {"type": "app_mention"}}', "missing_event_id"),
            (None, "not_json"),
        ],
    )
    def test_malformed_bodies(self, body, reason):
        with pytest.raises(SlackEventError) as exc:
            parse_event(body)
        assert exc.value.reason == reason


class TestParseRequest:
    def test_verified_then_parsed(self):
        body = body_of("message.im")
        event = parse_request(headers(body, **{"X-Slack-Retry-Num": "1"}), body, SECRET, now=NOW)
        assert isinstance(event, Message) and event.retry_num == 1

    def test_a_bad_signature_is_refused_before_the_body_is_read(self):
        with pytest.raises(SlackSignatureError):
            parse_request(headers(b"{}"), b"not json", SECRET, now=NOW)

    def test_a_signed_malformed_body(self):
        with pytest.raises(SlackEventError):
            parse_request(headers(b"[]"), b"[]", SECRET, now=NOW)

    def test_url_verification_is_signed_too(self):
        body = body_of("url_verification")
        assert isinstance(parse_request(headers(body), body, SECRET, now=NOW), UrlVerification)
        with pytest.raises(SlackSignatureError):
            parse_request({}, body, SECRET, now=NOW)


class TestEnvelopes:
    @pytest.mark.parametrize(
        "body, slug",
        [
            (EVENTS["app_mention"], "SLACK_APP_MENTION_RECEIVED"),
            (EVENTS["message.im"], "SLACK_DIRECT_MESSAGE_RECEIVED"),
            (variant("message.im", channel="C0123", channel_type="channel"), "SLACK_CHANNEL_MESSAGE_RECEIVED"),
            (variant("message.im", channel="G0123", channel_type="group"), "SLACK_CHANNEL_MESSAGE_RECEIVED"),
            (variant("message.im", channel="G0123", channel_type="mpim"), "SLACK_CHANNEL_MESSAGE_RECEIVED"),
            (variant("message.im", subtype="file_share"), "SLACK_DIRECT_MESSAGE_RECEIVED"),
            (EVENTS["reaction_added"], "SLACK_MESSAGE_REACTION_ADDED"),
            (EVENTS["reaction_removed"], "SLACK_MESSAGE_REACTION_REMOVED"),
            (
                variant("app_uninstalled", type="channel_created", channel={"id": "C0NEW", "name": "launch",
                                                                          "created": 1360782804, "creator": "U1"}),
                "SLACK_CHANNEL_CREATED",
            ),
            (variant("message.im", subtype="message_changed"), None),
            (variant("message.im", subtype="message_deleted"), None),
            (variant("message.im", channel_type=None), None),
            (variant("reaction_added", item={"type": "file", "file": "F0123"}), None),
            (EVENTS["tokens_revoked"], None),
            (EVENTS["app_uninstalled"], None),
        ],
    )
    def test_trigger_slugs(self, body, slug):
        event = parse_event(body)
        assert isinstance(event, EventCallback)
        assert trigger_slug(event) == slug
        assert event_to_envelope(event).metadata["trigger_slug"] == slug

    def test_message_envelope(self):
        event = parse_event(body_of("message.im"))
        assert isinstance(event, EventCallback)
        envelope = event_to_envelope(event)
        assert envelope.content_type == "application/json"
        assert envelope.source_id == "slack"
        # Composio's payload field names.
        assert envelope.data == {
            "channel": "D024BE91L",
            "channel_type": "im",
            "user": "U2147483697",
            "text": "Hello hello can you hear me?",
            "ts": "1355517523.000005",
            "team_id": "T123ABC456",
        }
        assert envelope.metadata == {
            "trigger_slug": "SLACK_DIRECT_MESSAGE_RECEIVED",
            "event_id": "Ev0PV52K21",
            "event_type": "message",
            "team_id": "T123ABC456",
            "api_app_id": "A0PNCHHK2",
            "event_time": 1355517523,
            "is_from_bot": False,
        }

    def test_reaction_envelope(self):
        event = parse_event(body_of("reaction_added"))
        assert isinstance(event, EventCallback)
        assert event_to_envelope(event).data == {
            "user": "U123ABC456",
            "reaction": "slightly_smiling_face",
            "message_channel": "C123ABC456",
            "message_ts": "1464196127.000002",
            "message_user": "U222222222",
            "event_ts": "1465244570.336841",
            "team_id": "T123ABC456",
        }

    def test_channel_created_envelope(self):
        channel = {"id": "C0NEW", "name": "launch", "created": 1360782804, "creator": "U1", "is_shared": False}
        event = parse_event(variant("app_uninstalled", type="channel_created", channel=channel))
        assert isinstance(event, EventCallback)
        assert event_to_envelope(event).data == {"id": "C0NEW", "name": "launch", "created": 1360782804, "creator": "U1"}

    @pytest.mark.parametrize(
        "body, from_bot",
        [
            (EVENTS["app_mention"], False),
            (variant("app_mention", bot_id="B0BOT"), True),
            (variant("message.im", bot_id="B0BOT"), True),
            (variant("message.im", subtype="bot_message"), True),
        ],
    )
    def test_messages_say_whether_a_bot_posted_them(self, body, from_bot):
        event = parse_event(body)
        assert isinstance(event, EventCallback)
        assert event_to_envelope(event).metadata["is_from_bot"] is from_bot

    def test_an_untriggered_event_carries_the_event_as_sent(self):
        event = parse_event(body_of("tokens_revoked"))
        assert isinstance(event, EventCallback)
        envelope = event_to_envelope(event)
        assert envelope.data == EVENTS["tokens_revoked"]["event"]
        assert envelope.metadata["trigger_slug"] is None
        assert envelope.metadata["event_id"] == "EvXXXXXXXX"
        assert "is_from_bot" not in envelope.metadata
