"""Tests for EisenbergCamera.stream_source.

Guards the streaming-path decision behind #23. Default (native): go2rtc reads
Arlo's rtsps directly — HEVC passthrough, no ffmpeg, smooth. That default MUST
be preserved: forcing every install through ffmpeg was a regression (an extra
`-c copy` remux hop made a smooth stream choppy on boxes where native worked).

The ``ffmpeg_stream`` option opts a single install into the ffmpeg source, for
boxes where go2rtc's native RTSP client can't read Arlo at all (black view).
It only takes effect when go2rtc is actually loaded; without go2rtc the legacy
PyAV path needs a bare URL it can open.

Exercises the real stream_source() without a running HA instance by building
the entity via __new__ and stubbing the coordinator's session-retry wrapper,
the config-entry options, and hass.config.components.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from custom_components.eisenberg.camera import EisenbergCamera
from eisenberg import DeviceInfo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

DEVICE_ID = "AGF14174D0019"
# Arlo advertises the stream as plain rtsp:// on the TLS port; the client
# hands us that back and we rewrite the scheme.
ARLO_URL = "rtsp://ip2:443/vzmodulelive/AGF14174D0019_1784116137452?egressToken=tok"
RTSPS_URL = "rtsps://ip2:443/vzmodulelive/AGF14174D0019_1784116137452?egressToken=tok"


def _device(device_id: str) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Camera {device_id}",
            "modelId": "VMC3052A",
            "xCloudId": "CLOUD123",
        }
    )


def _camera(
    components: set[str],
    *,
    options: dict[str, object] | None = None,
    start_url: str | None = ARLO_URL,
) -> EisenbergCamera:
    """An EisenbergCamera with HA/coordinator machinery stubbed."""

    async def call_with_session_retry(
        _name: str, factory: Callable[[], Awaitable[object]]
    ) -> object:
        # The real coordinator awaits the factory's coroutine; mirror that so
        # a raising client propagates exactly as in production.
        return await factory()

    async def start_stream(_device_id: str) -> object:
        if start_url is None:
            raise RuntimeError("boom")
        return SimpleNamespace(url=start_url)

    coordinator = SimpleNamespace(
        call_with_session_retry=call_with_session_retry,
        client=SimpleNamespace(start_stream=start_stream),
        entry=SimpleNamespace(options=options or {}),
    )

    camera = EisenbergCamera.__new__(EisenbergCamera)
    camera.coordinator = coordinator  # type: ignore[attr-defined]
    camera._device = _device(DEVICE_ID)
    camera.hass = SimpleNamespace(config=SimpleNamespace(components=components))  # type: ignore[attr-defined]
    return camera


@pytest.mark.asyncio
async def test_stream_source_default_is_bare_url_native() -> None:
    """Regression guard: default (option unset) → bare rtsps, go2rtc reads it
    natively. Forcing ffmpeg here made smooth streams choppy (#23 follow-up)."""
    camera = _camera({"camera", "go2rtc", "stream"})
    assert await camera.stream_source() == RTSPS_URL


@pytest.mark.asyncio
async def test_stream_source_ffmpeg_when_opted_in_and_go2rtc() -> None:
    """Opt-in ffmpeg_stream + go2rtc loaded → ffmpeg-wrapped source (#23 fix)."""
    camera = _camera({"camera", "go2rtc", "stream"}, options={"ffmpeg_stream": True})
    assert await camera.stream_source() == f"ffmpeg:{RTSPS_URL}"


@pytest.mark.asyncio
async def test_stream_source_opt_in_ignored_without_go2rtc() -> None:
    """Opt-in but no go2rtc → still bare URL, so the PyAV worker can open it."""
    camera = _camera({"camera", "stream"}, options={"ffmpeg_stream": True})
    assert await camera.stream_source() == RTSPS_URL


@pytest.mark.asyncio
async def test_stream_source_explicit_false_is_native() -> None:
    """Option explicitly False behaves like the default (native)."""
    camera = _camera({"camera", "go2rtc"}, options={"ffmpeg_stream": False})
    assert await camera.stream_source() == RTSPS_URL


@pytest.mark.asyncio
async def test_stream_source_none_on_start_failure() -> None:
    """A failed startStream returns None (no source), never a partial URL."""
    camera = _camera({"camera", "go2rtc"}, options={"ffmpeg_stream": True}, start_url=None)
    assert await camera.stream_source() is None
