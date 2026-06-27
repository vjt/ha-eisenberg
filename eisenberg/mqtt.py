"""MQTT event stream over WebSocket.

Manages the persistent WebSocket connection to Arlo's MQTT broker,
handles MQTT protocol packets, and dispatches parsed PUBLISH messages
to registered topic handlers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from .models import SubscribeOutcome, TopicResult
from .mqtt_codec import (
    build_connect,
    build_disconnect,
    build_pingreq,
    build_subscribe,
    parse_connack,
    parse_packet_type,
    parse_publish,
    parse_suback,
    split_packets,
    take_packet,
)

_LOGGER = logging.getLogger(__name__)

# MQTT packet types
CONNACK = 2
PUBLISH = 3
SUBACK = 9
PINGRESP = 13

# SUBACK return code signalling the broker refused a topic filter.
SUBACK_FAILURE = 0x80

# Handler type: async callback(topic, payload_dict)
EventHandler = Callable[[str, dict[str, Any]], Any]


def build_subscribe_topics(
    x_cloud_ids: list[str],
    user_id: str,
    extra_topics: list[str],
) -> list[str]:
    """Assemble the ordered, de-duplicated MQTT topic filter list.

    Combines the broad per-base wildcards (`d/{xCloudId}/out/#`) and the
    user wildcard (`u/{userId}/in/#`) with each device's own declared
    `allowedMqttTopics`. The device-declared filters are the authoritative
    set for doorbells and base-less cameras whose events live under a
    topic root our wildcards don't cover; subscribing to both is harmless
    (the broker just ACKs each filter independently). Order is stable so
    SUBACK return codes line up and MQTT logs stay diffable across runs.
    """
    topics: list[str] = [f"d/{cid}/out/#" for cid in x_cloud_ids]
    topics.append(f"u/{user_id}/in/#")
    for topic in extra_topics:
        if topic not in topics:
            topics.append(topic)
    return topics


class TopicRouter:
    """Routes MQTT topics to registered handlers.

    Supports MQTT-style wildcards:
    - `+` matches exactly one level
    - `#` matches zero or more levels (must be last)
    """

    def __init__(self) -> None:
        self._routes: list[tuple[list[str], EventHandler]] = []

    def register(self, pattern: str, handler: EventHandler) -> None:
        """Register a handler for a topic pattern."""
        self._routes.append((pattern.split("/"), handler))

    def match(self, topic: str) -> list[EventHandler]:
        """Find all handlers matching a topic."""
        parts = topic.split("/")
        matched: list[EventHandler] = []
        for pattern_parts, handler in self._routes:
            if self._matches(pattern_parts, parts):
                matched.append(handler)
        return matched

    @staticmethod
    def _matches(pattern: list[str], topic: list[str]) -> bool:
        for i, p in enumerate(pattern):
            if p == "#":
                return True  # Matches rest
            if i >= len(topic):
                return False
            if p != "+" and p != topic[i]:
                return False
        return len(pattern) == len(topic)


class MQTTEventStream:
    """Persistent MQTT connection over WebSocket to Arlo's broker.

    Usage:
        stream = MQTTEventStream(mqtt_url, user_id, token, [x_cloud_id], extra_topics)
        stream.on("d/+/out/cameras/+/is", handle_camera_state)
        await stream.connect()
        # ... runs until disconnect
        await stream.disconnect()
    """

    def __init__(
        self,
        mqtt_url: str,
        user_id: str,
        token: str,
        x_cloud_ids: list[str],
        extra_topics: list[str],
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not x_cloud_ids:
            raise ValueError("x_cloud_ids must not be empty")
        self._mqtt_url = mqtt_url
        self._user_id = user_id
        self._token = token
        self._x_cloud_ids = x_cloud_ids
        self._extra_topics = extra_topics
        self._session = http_session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._router = TopicRouter()
        self._running = False
        self._listen_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._on_disconnect: Callable[[], Any] | None = None
        # Inbound byte accumulator. MQTT packets are not aligned to WS frame
        # boundaries, so we buffer and decode complete packets ourselves.
        self._rx_buffer = bytearray()
        # Per-topic SUBACK outcome of the last connect(), for the coordinator
        # to log a grant/refuse/user-topic summary in a reachable namespace.
        self.subscribe_outcome: SubscribeOutcome | None = None

    def on(self, topic_pattern: str, handler: EventHandler) -> None:
        """Register a handler for a topic pattern."""
        self._router.register(topic_pattern, handler)

    def on_disconnect(self, callback: Callable[[], Any]) -> None:
        """Register a callback for when the connection drops."""
        self._on_disconnect = callback

    async def connect(self) -> None:
        """Connect to MQTT broker, subscribe, and start listening."""
        # Drop any bytes left over from a previous session — a reconnect on
        # the same instance must start framing from a clean slate.
        self._rx_buffer = bytearray()
        owns_session = False
        if self._session is None:
            self._session = aiohttp.ClientSession()
            owns_session = True

        try:
            self._ws = await self._session.ws_connect(
                f"{self._mqtt_url}/mqtt",
                protocols=["mqtt"],
                headers={
                    "Origin": "https://my.arlo.com",
                    "User-Agent": "Mozilla/5.0",
                },
            )
        except Exception:
            if owns_session:
                await self._session.close()
            raise

        # MQTT CONNECT
        client_id = f"user_{self._user_id}_{int(asyncio.get_event_loop().time())}"
        connect_pkt = build_connect(
            client_id=client_id,
            username=self._user_id,
            password=self._token,
            keepalive=60,
        )
        await self._ws.send_bytes(connect_pkt)

        # Wait for CONNACK. Read through the same byte buffer as everything
        # else — MQTT packets aren't aligned to WS frames, so the handshake
        # must not assume one packet per frame either (issue #13).
        connack = await self._read_packet()
        rc = parse_connack(connack)
        if rc != 0:
            await self._ws.close()
            raise ConnectionError(f"MQTT CONNACK failed with rc={rc}")

        # SUBSCRIBE — one wildcard per unique xCloudId (accounts with
        # cameras on multiple base stations have multiple xCloudIds), the
        # user wildcard, plus each device's own declared allowedMqttTopics.
        # Doorbells and base-less cameras can publish under a topic root the
        # wildcards miss, so the device-declared filters are what actually
        # delivers their motion/battery/signal events.
        topics = build_subscribe_topics(self._x_cloud_ids, self._user_id, self._extra_topics)
        subscribe_pkt = build_subscribe(packet_id=1, topics=topics)
        await self._ws.send_bytes(subscribe_pkt)

        # Read until the SUBACK, dispatching any PUBLISH that races ahead of
        # it (and leaving anything coalesced *after* it in the buffer for the
        # listen loop). A 0x80 return code means an ACL refused that topic —
        # the silent failure mode behind "entities never update".
        while True:
            packet = await self._read_packet()
            if parse_packet_type(packet) == SUBACK:
                codes = parse_suback(packet)
                break
            await self._dispatch_packet(packet)
        self.subscribe_outcome = SubscribeOutcome(
            results=[
                TopicResult(topic=topic, code=code, granted=code != SUBACK_FAILURE)
                for topic, code in zip(topics, codes, strict=False)
            ]
        )
        for topic, code in zip(topics, codes, strict=False):
            if code == SUBACK_FAILURE:
                _LOGGER.warning("MQTT broker REFUSED subscription to %s", topic)
            else:
                _LOGGER.debug("MQTT subscribed to %s (QoS %d granted)", topic, code)
        if all(code == SUBACK_FAILURE for code in codes) and codes:
            _LOGGER.error(
                "MQTT broker refused ALL %d subscriptions — no events will arrive",
                len(codes),
            )

        _LOGGER.info("MQTT connected; %d topic filter(s) subscribed", len(topics))
        self._running = True

        # Start background tasks
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def disconnect(self) -> None:
        """Gracefully disconnect."""
        self._running = False

        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None

        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None

        if self._ws and not self._ws.closed:
            await self._ws.send_bytes(build_disconnect())
            await self._ws.close()
            self._ws = None

    async def _read_packet(self) -> bytes:
        """Return the next complete MQTT packet, buffering across WS frames.

        Used during the connect handshake so CONNACK/SUBACK reads don't
        assume one packet per frame and don't discard coalesced trailing
        bytes (issue #13). Raises ConnectionError if the socket closes first.
        """
        while True:
            packet, rest = take_packet(bytes(self._rx_buffer))
            if packet is not None:
                self._rx_buffer = bytearray(rest)
                return packet
            if self._ws is None:
                raise ConnectionError("MQTT socket gone during handshake")
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.BINARY:
                self._rx_buffer += msg.data
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise ConnectionError("MQTT socket closed during handshake")

    async def _drain_buffer(self) -> None:
        """Dispatch every complete packet sitting in the buffer."""
        packets, remainder = split_packets(bytes(self._rx_buffer))
        self._rx_buffer = bytearray(remainder)
        for packet in packets:
            await self._dispatch_packet(packet)

    async def _listen_loop(self) -> None:
        """Read MQTT packets and dispatch PUBLISH messages."""
        # The handshake may have left packets in the buffer (e.g. a PUBLISH
        # coalesced into the SUBACK frame); flush them before blocking.
        await self._drain_buffer()
        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=90)
            except TimeoutError:
                _LOGGER.warning("MQTT receive timeout")
                break
            except asyncio.CancelledError:
                return

            if msg.type == aiohttp.WSMsgType.BINARY:
                # A single WS frame may hold several MQTT packets, or only
                # part of one. Buffer, then dispatch every complete packet
                # and keep the trailing partial for the next frame.
                self._rx_buffer += msg.data
                await self._drain_buffer()

            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                _LOGGER.warning("MQTT WebSocket closed/error")
                break

        self._running = False
        if self._on_disconnect:
            result = self._on_disconnect()
            if asyncio.iscoroutine(result):
                await result

    async def _dispatch_packet(self, data: bytes) -> None:
        """Route one complete MQTT packet to its handlers."""
        pkt_type = parse_packet_type(data)

        if pkt_type == PUBLISH:
            parsed = parse_publish(data)
            # Every inbound topic, before routing — the ground truth for
            # diagnosing "entities never update": shows exactly which
            # resources a device actually publishes on.
            _LOGGER.debug("MQTT recv topic=%s", parsed.topic)
            try:
                payload = json.loads(parsed.payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                _LOGGER.warning(
                    "Non-JSON MQTT payload on %s: %s",
                    parsed.topic,
                    parsed.payload[:200],
                )
                return

            handlers = self._router.match(parsed.topic)
            if handlers:
                for handler in handlers:
                    try:
                        result = handler(parsed.topic, payload)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        _LOGGER.exception(
                            "Error in MQTT handler for %s",
                            parsed.topic,
                        )
            else:
                _LOGGER.info(
                    "Unhandled MQTT topic %s: %s",
                    parsed.topic,
                    json.dumps(payload)[:500],
                )
        elif pkt_type == PINGRESP:
            pass  # Expected keepalive response
        else:
            _LOGGER.debug("MQTT packet type %d", pkt_type)

    async def _keepalive_loop(self) -> None:
        """Send PINGREQ every 50 seconds (keepalive is 60s)."""
        try:
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(50)
                if self._ws and not self._ws.closed:
                    await self._ws.send_bytes(build_pingreq())
        except asyncio.CancelledError:
            return
