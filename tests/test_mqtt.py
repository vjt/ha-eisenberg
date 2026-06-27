"""Tests for MQTT event stream dispatcher."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import aiohttp

from eisenberg.mqtt import MQTTEventStream, TopicRouter, build_subscribe_topics
from eisenberg.mqtt_codec import _encode_remaining_length


class TestBuildSubscribeTopics:
    def test_one_base_no_extras(self) -> None:
        topics = build_subscribe_topics(
            x_cloud_ids=["BASE-A"],
            user_id="USER",
            extra_topics=[],
        )
        assert topics == ["d/BASE-A/out/#", "u/USER/in/#"]

    def test_multiple_bases_preserve_order(self) -> None:
        topics = build_subscribe_topics(
            x_cloud_ids=["BASE-A", "BASE-B"],
            user_id="USER",
            extra_topics=[],
        )
        assert topics == [
            "d/BASE-A/out/#",
            "d/BASE-B/out/#",
            "u/USER/in/#",
        ]

    def test_device_topics_appended_and_deduped(self) -> None:
        # A doorbell whose own allowedMqttTopics name a resource our wildcards
        # would already cover, plus one that lands outside them.
        topics = build_subscribe_topics(
            x_cloud_ids=["BASE-A"],
            user_id="USER",
            extra_topics=[
                "d/BASE-A/out/#",  # duplicate of the wildcard — must collapse
                "d/OTHER-CLOUD/out/doorbells/FB1/#",
                "u/USER/in/#",  # duplicate of the user wildcard
            ],
        )
        assert topics == [
            "d/BASE-A/out/#",
            "u/USER/in/#",
            "d/OTHER-CLOUD/out/doorbells/FB1/#",
        ]


class TestTopicRouter:
    def test_register_and_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/cameras/+/is", handler)
        matched = router.match("d/CLOUD/out/cameras/CAM123/is")
        assert matched == [handler]

    def test_wildcard_hash(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/#", handler)
        matched = router.match("d/CLOUD/out/cameras/CAM/is")
        assert matched == [handler]

    def test_no_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/cameras/+/is", handler)
        matched = router.match("u/USER/in/feed/live")
        assert matched == []

    def test_multiple_handlers(self) -> None:
        router = TopicRouter()
        h1 = MagicMock()
        h2 = MagicMock()
        router.register("d/+/out/#", h1)
        router.register("d/+/out/cameras/+/is", h2)
        matched = router.match("d/CLOUD/out/cameras/CAM/is")
        assert h1 in matched
        assert h2 in matched

    def test_exact_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("u/USER/in/feed/live", handler)
        matched = router.match("u/USER/in/feed/live")
        assert matched == [handler]
        assert router.match("u/USER/in/feed/other") == []


# --- Handshake framing regression (issue #13) -------------------------------
#
# connect() used to read CONNACK and SUBACK as whole WS frames, discarding any
# trailing bytes. MQTT-over-WebSocket coalesces packets, so a PUBLISH riding in
# the same frame as the SUBACK (or arriving just before it) was silently lost.
# These drive connect() with a fake WebSocket and assert such a PUBLISH still
# reaches its handler.

CONNACK_OK = bytes([0x20, 0x02, 0x00, 0x00])
# SUBACK for the two filters connect() subscribes (base wildcard + user
# wildcard), both granted at QoS 0. remaining length 4 = 2-byte id + 2 codes.
SUBACK_TWO_GRANTED = bytes([0x90, 0x04, 0x00, 0x01, 0x00, 0x00])
# base wildcard granted (QoS 0), user wildcard refused (0x80).
SUBACK_GRANT_REFUSE = bytes([0x90, 0x04, 0x00, 0x01, 0x00, 0x80])
SNAPSHOT_TOPIC = "d/BASE/out/cameras/CAM/fullFrameSnapshotAvailable"


def _publish(topic: str, payload: bytes) -> bytes:
    tb = topic.encode()
    remaining = len(tb).to_bytes(2, "big") + tb + payload
    return bytes([0x30]) + _encode_remaining_length(len(remaining)) + remaining


class _FakeWS:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive(self) -> SimpleNamespace:
        if self._frames:
            return SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=self._frames.pop(0))
        self.closed = True
        return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=b"")

    async def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    async def ws_connect(self, *_args: Any, **_kwargs: Any) -> _FakeWS:
        return self._ws

    async def close(self) -> None:
        pass


async def _run_until_drained(stream: MQTTEventStream) -> None:
    """connect() then let the listen loop finish draining the fed frames."""
    await stream.connect()
    assert stream._listen_task is not None
    await asyncio.wait_for(stream._listen_task, timeout=2)
    await stream.disconnect()


async def _stream_with_frames(frames: list[bytes], received: list[str]) -> MQTTEventStream:
    ws = _FakeWS(frames)
    stream = MQTTEventStream(
        mqtt_url="wss://broker",
        user_id="USER",
        token="TOKEN",
        x_cloud_ids=["BASE"],
        extra_topics=[],
        http_session=_FakeSession(ws),  # type: ignore[arg-type]
    )
    stream.on(
        "d/+/out/cameras/+/fullFrameSnapshotAvailable",
        lambda topic, _payload: received.append(topic),
    )
    return stream


class TestHandshakeFraming:
    async def test_publish_coalesced_after_suback_not_dropped(self) -> None:
        received: list[str] = []
        stream = await _stream_with_frames(
            [CONNACK_OK, SUBACK_TWO_GRANTED + _publish(SNAPSHOT_TOPIC, b"{}")],
            received,
        )
        await _run_until_drained(stream)
        assert received == [SNAPSHOT_TOPIC]

    async def test_publish_before_suback_not_dropped(self) -> None:
        received: list[str] = []
        stream = await _stream_with_frames(
            [CONNACK_OK, _publish(SNAPSHOT_TOPIC, b"{}") + SUBACK_TWO_GRANTED],
            received,
        )
        await _run_until_drained(stream)
        assert received == [SNAPSHOT_TOPIC]

    async def test_connack_and_clean_subscribe_still_work(self) -> None:
        received: list[str] = []
        stream = await _stream_with_frames([CONNACK_OK, SUBACK_TWO_GRANTED], received)
        await _run_until_drained(stream)
        assert received == []  # nothing to deliver, but no crash either


class TestSubscribeOutcome:
    async def test_connect_records_per_topic_outcome(self) -> None:
        received: list[str] = []
        stream = await _stream_with_frames([CONNACK_OK, SUBACK_GRANT_REFUSE], received)
        await _run_until_drained(stream)

        outcome = stream.subscribe_outcome
        assert outcome is not None
        assert outcome.granted_count == 1
        assert outcome.refused_count == 1
        assert outcome.refused_topics == ["u/USER/in/#"]
        base = outcome.result_for("d/BASE/out/#")
        user = outcome.result_for("u/USER/in/#")
        assert base is not None and base.granted is True
        assert user is not None and user.granted is False
        assert user.code == 0x80
