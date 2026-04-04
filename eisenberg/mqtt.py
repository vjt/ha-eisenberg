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

from .mqtt_codec import (
    build_connect,
    build_disconnect,
    build_pingreq,
    build_subscribe,
    parse_connack,
    parse_packet_type,
    parse_publish,
)

_LOGGER = logging.getLogger(__name__)

# MQTT packet types
CONNACK = 2
PUBLISH = 3
SUBACK = 9
PINGRESP = 13

# Handler type: async callback(topic, payload_dict)
EventHandler = Callable[[str, dict[str, Any]], Any]


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
        matched = []
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
        stream = MQTTEventStream(mqtt_url, user_id, token, x_cloud_id)
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
        x_cloud_id: str,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._mqtt_url = mqtt_url
        self._user_id = user_id
        self._token = token
        self._x_cloud_id = x_cloud_id
        self._session = http_session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._router = TopicRouter()
        self._running = False
        self._listen_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._on_disconnect: Callable[[], Any] | None = None

    def on(self, topic_pattern: str, handler: EventHandler) -> None:
        """Register a handler for a topic pattern."""
        self._router.register(topic_pattern, handler)

    def on_disconnect(self, callback: Callable[[], Any]) -> None:
        """Register a callback for when the connection drops."""
        self._on_disconnect = callback

    async def connect(self) -> None:
        """Connect to MQTT broker, subscribe, and start listening."""
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

        # Wait for CONNACK
        msg = await self._ws.receive()
        data = msg.data if isinstance(msg.data, bytes) else msg.data.encode()
        rc = parse_connack(data)
        if rc != 0:
            await self._ws.close()
            raise ConnectionError(f"MQTT CONNACK failed with rc={rc}")

        # SUBSCRIBE
        topics = [
            f"d/{self._x_cloud_id}/out/#",
            f"u/{self._user_id}/in/#",
        ]
        subscribe_pkt = build_subscribe(packet_id=1, topics=topics)
        await self._ws.send_bytes(subscribe_pkt)

        # Wait for SUBACK
        await self._ws.receive()

        _LOGGER.info("MQTT connected and subscribed to %s", topics)
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

    async def _listen_loop(self) -> None:
        """Read MQTT packets and dispatch PUBLISH messages."""
        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=90)
            except TimeoutError:
                _LOGGER.warning("MQTT receive timeout")
                break
            except asyncio.CancelledError:
                return

            if msg.type == aiohttp.WSMsgType.BINARY:
                data = msg.data
                pkt_type = parse_packet_type(data)

                if pkt_type == PUBLISH:
                    parsed = parse_publish(data)
                    try:
                        payload = json.loads(parsed.payload)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        _LOGGER.warning(
                            "Non-JSON MQTT payload on %s: %s",
                            parsed.topic,
                            parsed.payload[:200],
                        )
                        continue

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

    async def _keepalive_loop(self) -> None:
        """Send PINGREQ every 50 seconds (keepalive is 60s)."""
        try:
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(50)
                if self._ws and not self._ws.closed:
                    await self._ws.send_bytes(build_pingreq())
        except asyncio.CancelledError:
            return
