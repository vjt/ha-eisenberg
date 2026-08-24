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

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from homeassistant.components.camera import Camera

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
    started: list[str] | None = None,
    delay: float = 0.0,
) -> EisenbergCamera:
    """An EisenbergCamera with HA/coordinator machinery stubbed.

    ``started`` records every device id handed to startStream, so a test can
    assert how many live streams Arlo was actually asked for. ``delay`` holds
    that call open, which is what lets a test overlap two callers the way the
    frontend does.
    """

    async def call_with_session_retry(
        _name: str, factory: Callable[[], Awaitable[object]]
    ) -> object:
        # The real coordinator awaits the factory's coroutine; mirror that so
        # a raising client propagates exactly as in production.
        return await factory()

    async def start_stream(device_id: str) -> object:
        if started is not None:
            started.append(device_id)
        if delay:
            await asyncio.sleep(delay)
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
    # __init__ is bypassed above; the per-entity lock is the one piece of
    # instance state stream_source needs that has no sane class default.
    camera._stream_lock = asyncio.Lock()
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


@pytest.mark.asyncio
async def test_provider_probe_does_not_start_a_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA calls stream_source() from async_refresh_providers just to read the
    scheme. Answering it for real wakes the camera — LED on, ~30 s clip
    uploaded — on every restart, reload and options change. The probe must be
    answered from the scheme alone, with Arlo untouched."""
    started: list[str] = []
    camera = _camera({"camera", "go2rtc", "stream"}, started=started)

    seen: list[str | None] = []

    async def fake_super_refresh(*_args: object, **_kwargs: object) -> None:
        # Stand in for HA's implementation, which asks the entity for a source
        # and compares its scheme against go2rtc's supported list.
        seen.append(await camera.stream_source())

    monkeypatch.setattr(Camera, "async_refresh_providers", fake_super_refresh)
    await camera.async_refresh_providers()

    assert started == []
    assert seen and seen[0] is not None
    assert seen[0].startswith("rtsps://")


@pytest.mark.asyncio
async def test_probe_answer_carries_the_ffmpeg_prefix() -> None:
    """The probe decides which provider HA picks, so its answer has to wear the
    same prefix the real source would — otherwise opting into ffmpeg silently
    changes which player HA offers."""
    camera = _camera({"camera", "go2rtc", "stream"}, options={"ffmpeg_stream": True})
    camera._probing = True
    source = await camera.stream_source()
    assert source is not None
    assert source.startswith("ffmpeg:rtsps://")


@pytest.mark.asyncio
async def test_overlapping_callers_share_one_arlo_stream() -> None:
    """Opening live view asks twice within milliseconds — the WebRTC offer and
    the HLS fallback. Arlo rejects the second startStream with 4006, killing
    whichever path drew it, so the pair has to share a single stream."""
    started: list[str] = []
    camera = _camera({"camera", "go2rtc", "stream"}, started=started, delay=0.05)

    first, second = await asyncio.gather(camera.stream_source(), camera.stream_source())

    assert started == [DEVICE_ID]
    assert first == RTSPS_URL
    assert second == RTSPS_URL


@pytest.mark.asyncio
async def test_stream_is_dropped_when_arlo_ends_the_session() -> None:
    """Arlo's egress URL dies with the stream, but HA hands the same Stream
    object to every later viewer. Keeping it means one failed live view breaks
    every later one until a restart, so the dead Stream is stopped and cleared
    once the final keyframe is saved."""
    stopped: list[bool] = []

    class _Stream:
        async def async_get_image(self) -> bytes:
            return b"jpeg"

        async def stop(self) -> None:
            stopped.append(True)

    archived: list[tuple[str, bytes, str]] = []

    async def archive_bytes(device_id: str, content: bytes, media_type: str) -> None:
        archived.append((device_id, content, media_type))

    camera = _camera({"camera", "go2rtc", "stream"})
    camera.coordinator.image_bytes = {}  # type: ignore[attr-defined]
    camera.coordinator.archive_bytes = archive_bytes  # type: ignore[attr-defined]
    camera.stream = _Stream()  # type: ignore[assignment]

    await camera._cache_last_stream_frame()

    assert camera.coordinator.image_bytes[DEVICE_ID] == b"jpeg"
    assert archived == [(DEVICE_ID, b"jpeg", "stream_thumb")]
    assert stopped == [True]
    assert camera.stream is None
