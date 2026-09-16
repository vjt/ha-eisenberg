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
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from homeassistant.components.camera import Camera

from custom_components.eisenberg.camera import _PROBING, EisenbergCamera
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
    # __init__ is bypassed above, so the instance state HA's own Camera.__init__
    # would have set has to be supplied here: the per-entity lock, and `stream`,
    # which Camera.__init__ initialises to None (camera/__init__.py:451).
    camera._stream_lock = asyncio.Lock()
    camera.stream = None  # type: ignore[assignment]
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
    token = _PROBING.set(True)
    try:
        source = await camera.stream_source()
    finally:
        _PROBING.reset(token)
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


@pytest.mark.asyncio
async def test_pyav_gets_a_bare_url_while_go2rtc_keeps_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ffmpeg:`` is go2rtc source syntax. HA's own stream worker is PyAV
    opening the URL itself, so it must be handed the bare one — otherwise
    opting into the ffmpeg source leaves the HLS player with nothing it can
    parse and no working fallback. The frontend opens both players at once, so
    a go2rtc caller running concurrently must still see the prefix."""
    started: list[str] = []
    camera = _camera(
        {"camera", "go2rtc", "stream"},
        options={"ffmpeg_stream": True},
        started=started,
        delay=0.05,
    )

    async def fake_super_create_stream(_self: object) -> str | None:
        # Stand in for HA's Stream construction, which asks the entity for a
        # source and hands it to the PyAV worker.
        return await camera.stream_source()

    monkeypatch.setattr(Camera, "async_create_stream", fake_super_create_stream)

    for_pyav, for_go2rtc = await asyncio.gather(
        camera.async_create_stream(),
        camera.stream_source(),
    )

    # One Arlo stream, two consumers, each told how to open it.
    assert started == [DEVICE_ID]
    assert for_pyav == RTSPS_URL
    assert for_go2rtc == f"ffmpeg:{RTSPS_URL}"


@pytest.mark.asyncio
async def test_probe_does_not_hand_its_fake_url_to_a_real_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe answer is a placeholder, and it must not escape its own caller.

    ``async_refresh_providers`` holds the probe flag across an await — HA's
    implementation reaches out to go2rtc for its supported schemes — and the
    frontend can open live view inside that window. Instance state would hand
    ``probe.invalid`` to that viewer, who then builds a Stream around a URL
    that resolves to nothing. The flag is per-task for the same reason the
    bare-source flag is.
    """
    started: list[str] = []
    camera = _camera({"camera", "go2rtc", "stream"}, started=started)

    probe_running = asyncio.Event()
    release = asyncio.Event()

    async def fake_super_refresh(*_args: object, **_kwargs: object) -> None:
        probe_running.set()
        await release.wait()
        await camera.stream_source()

    monkeypatch.setattr(Camera, "async_refresh_providers", fake_super_refresh)

    probe = asyncio.create_task(camera.async_refresh_providers())
    await probe_running.wait()
    # A real viewer arrives while the probe is still in flight.
    viewer_url = await camera.stream_source()
    release.set()
    await probe

    assert viewer_url == RTSPS_URL
    assert started == [DEVICE_ID], "the viewer, and only the viewer, woke the camera"


@pytest.mark.asyncio
async def test_url_cache_is_dropped_when_arlo_ends_the_session() -> None:
    """The 10 s reuse window must not outlive the stream it was cached from.

    Dropping the dead Stream is only half the fix: a viewer arriving inside
    the window would be handed the same retired egress URL from the cache and
    build a fresh Stream around it, which is the bug this was meant to end.
    """
    started: list[str] = []
    camera = _camera({"camera", "go2rtc", "stream"}, started=started)
    camera.coordinator.image_bytes = {}  # type: ignore[attr-defined]

    async def archive_bytes(device_id: str, content: bytes, media_type: str) -> None:
        return None

    camera.coordinator.archive_bytes = archive_bytes  # type: ignore[attr-defined]

    class _Stream:
        async def async_get_image(self) -> bytes:
            return b"jpeg"

        async def stop(self) -> None:
            return None

    assert await camera.stream_source() == RTSPS_URL
    camera.stream = _Stream()  # type: ignore[assignment]
    await camera._cache_last_stream_frame()

    # Immediately afterwards — well inside the 10 s window.
    assert await camera.stream_source() == RTSPS_URL
    assert started == [DEVICE_ID, DEVICE_ID], "the second viewer must get a fresh URL"


class _FakeStream:
    """A Stream that behaves like HA's: ``async_get_image`` starts the worker.

    That detail is the whole of #34. ``Stream.async_get_image`` does
    ``self.add_provider(HLS_PROVIDER)`` then ``await self.start()``, and
    ``start()`` spawns the worker thread whenever one is not alive — against
    whatever source the Stream was built with. On a stopped stream that is a
    retired Arlo egress URL, and the keyframe handed back is whatever the
    converter decoded last, however long ago.
    """

    def __init__(self, frame: bytes = b"old-keyframe") -> None:
        self.frame = frame
        self.starts = 0
        self.stopped = False

    async def async_get_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        self.starts += 1
        return self.frame

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_idle_stream_does_not_beat_a_fresher_snapshot() -> None:
    """#34: a keyframe from a finished session must not outrank a new snapshot.

    peteramelang's yard camera served a DAYLIGHT frame at 20:01 while its
    neighbours were on night infrared, with a newer snapshot already sitting
    in the archive. A Stream object existing is not evidence that it is
    producing frames.
    """
    camera = _camera({"camera", "go2rtc", "stream"})
    camera.coordinator.image_bytes = {DEVICE_ID: b"fresh-snapshot"}  # type: ignore[attr-defined]
    camera.stream = _FakeStream(b"old-keyframe")  # type: ignore[assignment]
    camera._attr_is_streaming = False

    assert await camera.async_camera_image() == b"fresh-snapshot"


@pytest.mark.asyncio
async def test_idle_stream_does_not_overwrite_the_cached_snapshot() -> None:
    """The stale keyframe was also written over the fresh bytes, so the
    snapshot was not merely ignored — it was destroyed, for every later
    reader too."""
    camera = _camera({"camera", "go2rtc", "stream"})
    camera.coordinator.image_bytes = {DEVICE_ID: b"fresh-snapshot"}  # type: ignore[attr-defined]
    camera.stream = _FakeStream(b"old-keyframe")  # type: ignore[assignment]
    camera._attr_is_streaming = False

    await camera.async_camera_image()

    assert camera.coordinator.image_bytes[DEVICE_ID] == b"fresh-snapshot"


@pytest.mark.asyncio
async def test_idle_stream_is_not_restarted_by_a_tile_refresh() -> None:
    """``async_get_image`` starts the worker, so consulting a finished stream
    reopened a retired Arlo URL on every tile poll — a camera wake attempt and
    the restart-backoff loop, both of which 0.4.3 exists to prevent."""
    camera = _camera({"camera", "go2rtc", "stream"})
    camera.coordinator.image_bytes = {DEVICE_ID: b"fresh-snapshot"}  # type: ignore[attr-defined]
    stream = _FakeStream()
    camera.stream = stream  # type: ignore[assignment]
    camera._attr_is_streaming = False

    await camera.async_camera_image()

    assert stream.starts == 0


@pytest.mark.asyncio
async def test_live_stream_still_wins_and_refreshes_the_cache() -> None:
    """While Arlo reports the stream active the keyframe IS the freshest thing
    there is — that is the case the branch was written for, and it has to keep
    working, including for a disarmed camera where Arlo refuses snapshots."""
    camera = _camera({"camera", "go2rtc", "stream"})
    camera.coordinator.image_bytes = {DEVICE_ID: b"older-snapshot"}  # type: ignore[attr-defined]
    camera.stream = _FakeStream(b"live-keyframe")  # type: ignore[assignment]
    camera._attr_is_streaming = True

    assert await camera.async_camera_image() == b"live-keyframe"
    assert camera.coordinator.image_bytes[DEVICE_ID] == b"live-keyframe"


@pytest.mark.asyncio
async def test_a_failed_live_view_does_not_poison_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live view that never starts leaves a Stream nothing will ever clear.

    0.4.3 drops the dead Stream on the streaming-to-idle transition, but that
    transition needs ``userStreamActive`` to have arrived first. When the
    stream never starts it never does, so the Stream built around that failed
    URL survives and ``async_create_stream`` hands the same one to every later
    viewer — forever, until a restart. Once the reuse window has lapsed with
    the camera not streaming, the Stream belongs to a session that is over and
    must not be handed on.
    """
    camera = _camera({"camera", "go2rtc", "stream"})
    camera._attr_is_streaming = False
    dead = _FakeStream()
    camera.stream = dead  # type: ignore[assignment]
    # The URL it was built from was minted longer ago than the reuse window.
    camera._last_url = (monotonic() - 3600, ARLO_URL)

    built: list[str | None] = []

    async def fake_super_create_stream(_self: object) -> object | None:
        built.append(await camera.stream_source())
        camera.stream = _FakeStream(b"new")  # type: ignore[assignment]
        return camera.stream

    monkeypatch.setattr(Camera, "async_create_stream", fake_super_create_stream)

    result = await camera.async_create_stream()

    assert dead.stopped, "the dead Stream must be stopped, not merely dropped"
    assert result is not dead
    assert built == [RTSPS_URL], "the replacement is built around a fresh URL"


@pytest.mark.asyncio
async def test_the_paired_frontend_requests_still_share_one_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not undo #33: within the reuse window the Stream the
    first caller just built is still the live one, even though Arlo has not
    reported ``userStreamActive`` yet."""
    camera = _camera({"camera", "go2rtc", "stream"})
    camera._attr_is_streaming = False
    fresh = _FakeStream()
    camera.stream = fresh  # type: ignore[assignment]
    camera._last_url = (monotonic(), ARLO_URL)

    async def fake_super_create_stream(_self: object) -> object | None:
        return camera.stream

    monkeypatch.setattr(Camera, "async_create_stream", fake_super_create_stream)

    assert await camera.async_create_stream() is fresh
    assert not fresh.stopped


@pytest.mark.asyncio
async def test_a_running_stream_is_never_torn_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long live view outlives the reuse window; while Arlo says the stream
    is running it is the live one whatever the cache says."""
    camera = _camera({"camera", "go2rtc", "stream"})
    camera._attr_is_streaming = True
    live = _FakeStream()
    camera.stream = live  # type: ignore[assignment]
    camera._last_url = (monotonic() - 3600, ARLO_URL)

    async def fake_super_create_stream(_self: object) -> object | None:
        return camera.stream

    monkeypatch.setattr(Camera, "async_create_stream", fake_super_create_stream)

    assert await camera.async_create_stream() is live
    assert not live.stopped
