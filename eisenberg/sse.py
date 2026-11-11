"""Arlo server-sent event stream (`/hmsweb/client/subscribe`).

The same account events MQTT carries, as JSON frames over one long-lived
HTTP response — but with no per-topic ACL. Used only as a supplement: some
brokers refuse a topic a device actually publishes on (a Gen2 doorbell's
`cameras/<id>` motion, issue #38), and SSE is how AArlo/pyaarlo hears it.

The stream owns its own reconnect loop: a dropped or refused connection is
retried on the given backoff, each attempt with freshly built headers so a
refreshed token is picked up. A failure here never touches MQTT.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .redact import loggable

_LOGGER = logging.getLogger(__name__)

# Handler type: async callback(payload_dict)
SSEHandler = Callable[[dict[str, Any]], Awaitable[None]]


class SSEParser:
    """Incremental text/event-stream decoder yielding each event's data."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._data: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        """Consume a chunk; return the data of every event it completed."""
        self._buffer += self._decoder.decode(chunk)
        events: list[str] = []
        while True:
            line, sep, rest = self._buffer.partition("\n")
            if not sep:
                break
            self._buffer = rest
            line = line.removesuffix("\r")
            if not line:
                if self._data:
                    events.append("\n".join(self._data))
                    self._data = []
                continue
            field, _, value = line.partition(":")
            if field == "data":
                self._data.append(value.removeprefix(" "))
        return events


class SSEEventStream:
    """Reconnecting consumer of Arlo's SSE endpoint.

    Usage:
        stream = SSEEventStream(url, headers_factory, session, on_event, backoff, 300.0)
        stream.start()
        # ... payloads arrive at on_event until
        await stream.stop()
    """

    def __init__(
        self,
        url: str,
        headers_factory: Callable[[], dict[str, str]],
        http_session: aiohttp.ClientSession,
        on_event: SSEHandler,
        backoff: tuple[float, ...],
        read_timeout: float,
    ) -> None:
        if not backoff:
            raise ValueError("backoff must not be empty")
        self._url = url
        self._headers_factory = headers_factory
        self._session = http_session
        self._on_event = on_event
        self._backoff = backoff
        self._read_timeout = read_timeout
        self._task: asyncio.Task[None] | None = None
        # True while reconnecting after a healthy session ended; those
        # reconnects happen every few minutes and are logged quietly.
        self._routine_reconnect = False

    def start(self) -> None:
        """Start the background read/reconnect loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the loop and drop the connection."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                delivered = await self._read_session()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("SSE event stream failed: %s", err)
                delivered = False
            self._routine_reconnect = delivered
            if delivered:
                # A healthy session that ended (idle timeout, logout, server
                # close): reconnect at once. Any pause here is a window in
                # which the events this stream exists for are lost (#38).
                failures = 0
                continue
            delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            await asyncio.sleep(delay)

    async def _read_session(self) -> bool:
        """Read one connection until it ends. True if it carried any frame."""
        resp = await self._session.get(
            self._url,
            headers={
                **self._headers_factory(),
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            },
            timeout=aiohttp.ClientTimeout(total=None, sock_read=self._read_timeout),
        )
        try:
            if resp.status != 200:
                raise ConnectionError(f"HTTP {resp.status}")
            parser = SSEParser()
            delivered = False
            chunks = resp.content.iter_any()
            while True:
                try:
                    chunk = await anext(chunks)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    # Arlo sends nothing at all on an idle stream, not even a
                    # keepalive, so the read timeout firing is routine: it is
                    # only how a silently dead connection gets noticed.
                    _LOGGER.debug(
                        "SSE event stream idle for %ss; reconnecting", self._read_timeout
                    )
                    return True
                for data in parser.feed(chunk):
                    delivered = True
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        _LOGGER.warning("Non-JSON SSE frame: %s", data[:200])
                        continue
                    _LOGGER.debug("SSE frame: %s", loggable(payload, 500))
                    if not isinstance(payload, dict):
                        _LOGGER.warning("Non-object SSE frame: %s", loggable(payload, 200))
                        continue
                    frame: dict[str, Any] = payload  # pyright: ignore[reportUnknownVariableType]
                    if frame.get("status") == "connected":
                        _LOGGER.log(
                            logging.DEBUG if self._routine_reconnect else logging.INFO,
                            "SSE event stream connected",
                        )
                        continue
                    if frame.get("action") == "logout":
                        # Arlo ended this session (a token refresh or a login
                        # elsewhere). Reconnect with whatever token is current.
                        _LOGGER.info("SSE event stream logged out by Arlo; reconnecting")
                        return True
                    try:
                        await self._on_event(frame)
                    except Exception:
                        _LOGGER.exception("Error in SSE handler for %s", loggable(frame, 200))
            _LOGGER.debug("SSE event stream ended by server; reconnecting")
            return delivered
        finally:
            resp.release()
