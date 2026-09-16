"""Camera platform for Eisenberg."""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from time import monotonic
from typing import Any, ClassVar

import aiohttp
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.stream import Stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg import DeviceInfo

from .const import CONF_FFMPEG_STREAM, DEFAULT_FFMPEG_STREAM
from .coordinator import EisenbergCoordinator

SERVICE_SNAPSHOT = "snapshot"

# Set while HA's own stream component is building a Stream. The ``ffmpeg:``
# prefix is go2rtc source syntax; the PyAV worker behind HLS opens the URL
# itself and cannot parse it. A ContextVar and not an attribute because the
# frontend opens both players at once — the go2rtc offer runs in its own task
# and must keep seeing the prefixed source.
_BARE_SOURCE: ContextVar[bool] = ContextVar("eisenberg_bare_source", default=False)

# Set while HA is probing this entity for a WebRTC provider and wants nothing
# from the source but its scheme. A ContextVar for the same reason as above,
# and a stronger one: the probe holds the flag across an await (HA asks go2rtc
# for its supported schemes), and a viewer opening live view inside that window
# would otherwise be handed the placeholder URL below and build a Stream around
# a host that does not resolve.
_PROBING: ContextVar[bool] = ContextVar("eisenberg_probing", default=False)

# Handed to the provider probe in place of a real egress URL. Only its scheme
# is ever read; the host is deliberately unroutable so a leak fails loudly
# rather than quietly streaming from somewhere unexpected.
_PROBE_URL = "rtsps://probe.invalid/"

# How long a freshly minted Arlo egress URL is handed out again instead of
# asking for another. Long enough for the pair of requests the frontend fires
# when live view opens (Arlo refuses the second startStream with 4006), short
# enough that a real retry gets a fresh token. It doubles as the grace period
# below: inside it, a Stream is assumed to belong to the session being opened.
STREAM_URL_REUSE_SECONDS = 10.0

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Eisenberg cameras."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(EisenbergCamera(coordinator, device) for device in coordinator.cameras)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SNAPSHOT,
        {},
        "async_request_snapshot",
    )


class EisenbergCamera(CoordinatorEntity[EisenbergCoordinator], Camera):
    """Arlo camera entity with snapshot and RTSP stream support."""

    _attr_has_entity_name = True
    _attr_name = None  # Use device name
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_is_streaming: bool = False
    _attr_motion_detection_enabled: bool = True
    _last_url: tuple[float, str] | None = None
    # Arlo's stream is RTSP-over-TLS on port 443; ffmpeg's default UDP
    # transport can't traverse TLS so it reads garbage and fails with
    # "Invalid data found when processing input". Force TCP. The other
    # flags shave seconds off the HLS pipeline lag — fflags=nobuffer
    # disables ffmpeg's input buffer, flags=low_delay tells decoders not
    # to look ahead, and use_wallclock_as_timestamps stops the worker
    # from re-sequencing PTS (Arlo/Wowza sometimes ships drifty stamps).
    _attr_stream_options: ClassVar[dict[str, str | bool | float]] = {
        "rtsp_transport": "tcp",
        "fflags": "nobuffer",
        "flags": "low_delay",
        "use_wallclock_as_timestamps": True,
    }

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)

        self._device = device
        self._stream_lock = asyncio.Lock()
        self._attr_unique_id = f"{device.device_id}_camera"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
            "name": device.device_name,
            "manufacturer": "Arlo",
            "model": device.model_id,
        }

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest camera image.

        Preference order:
        1. A live keyframe from HA's stream worker, but only while Arlo
           reports the stream actually running — that is the one window in
           which the keyframe is the freshest image there is, and it is how
           the tile refreshes on a disarmed camera (Arlo refuses on-demand
           snapshots then).
        2. Bytes cached by the coordinator from MQTT-delivered URLs.
        3. Refetch from the most recent snapshot/thumbnail URL.

        The gate is the whole of #34. A Stream object existing says nothing
        about whether it is producing frames: it is built on the first live
        view and survives the session. Consulting it afterwards returned
        whatever keyframe the converter decoded last — peteramelang's yard
        camera served a daylight frame at 20:01 while its neighbours were on
        night infrared, with a newer snapshot already in the archive — and
        then wrote that corpse over the fresher bytes in ``image_bytes``, so
        the snapshot was not merely lost to this caller but to every later
        one. Worse, ``Stream.async_get_image`` does ``await self.start()``,
        which respawns the worker thread against the source the Stream was
        built with. On a finished session that is a retired Arlo egress URL,
        so every tile poll reopened it: a camera wake attempt and the
        restart-backoff loop, both of which 0.4.3 exists to prevent.

        "Just after live view" is still covered, and by the right mechanism:
        ``_cache_last_stream_frame`` grabs the final keyframe on the
        streaming-to-idle transition and stores it in ``image_bytes``, where
        a later snapshot can legitimately supersede it.
        """
        if self.stream is not None and self._attr_is_streaming:
            try:
                frame = await self.stream.async_get_image(width=width, height=height)
            except Exception:
                _LOGGER.debug("Stream keyframe extraction failed", exc_info=True)
                frame = None
            if frame:
                self.coordinator.image_bytes[self._device.device_id] = frame
                return frame

        cached = self.coordinator.image_bytes.get(self._device.device_id)
        if cached is not None:
            return cached

        url = self.coordinator.latest_thumbnails.get(self._device.device_id)
        if not url:
            url = self.coordinator.latest_snapshots.get(self._device.device_id)
        if not url:
            return None

        try:
            async with aiohttp.ClientSession() as session, session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception:
            _LOGGER.debug("Failed to fetch camera image from %s", url)

        return None

    async def async_request_snapshot(self) -> None:
        """Ask Arlo for a fresh full-frame snapshot.

        Delegates to the coordinator so the `eisenberg.snapshot` service and
        the per-camera snapshot button (#8) share one standby-guard and
        session-retry path. The image arrives later via MQTT
        (fullFrameSnapshotAvailable) and the coordinator caches/archives it.
        """
        await self.coordinator.request_snapshot(self._device.device_id)

    async def stream_source(self) -> str | None:
        """Return the live stream source URL.

        Arlo serves the stream as RTSP-over-TLS on port 443 but advertises it
        as plain rtsp://; rewrite the scheme to rtsps:// (matches pyaarlo) so
        the consumer negotiates TLS.

        By default we return the bare rtsps URL. On a go2rtc box (HA default
        since 2024.11) go2rtc reads it with its *native* RTSP client — in
        process, HEVC passthrough, no ffmpeg, no transcode, smooth. That is the
        right path and must stay the default.

        The ``ffmpeg_stream`` option opts an install into routing the source
        through ffmpeg (``ffmpeg:rtsps://…``) instead. It exists only for boxes
        where go2rtc's native RTSP client can't read Arlo's stream at all —
        black live view, "RTSP wrong input" / "RTP header size insufficient:
        0 < 4" (issue #23). ffmpeg reads Arlo's TLS stream correctly, at the
        cost of an extra ``-c copy`` remux hop that adds jitter on boxes where
        native already worked — hence opt-in, not blanket. Only honoured when
        go2rtc is actually loaded; otherwise the legacy PyAV path needs a bare
        URL it can open.

        Two callers ask for a source that nobody is going to watch, and both
        used to wake the camera: the provider probe (see
        ``async_refresh_providers``) and the second of the pair of requests
        the frontend fires when live view opens. Both are answered without
        touching Arlo.
        """
        if _PROBING.get():
            # Scheme-only answer for the probe — HA compares the prefix
            # against go2rtc's supported list and throws the URL away.
            return self._with_transport(_PROBE_URL)

        async with self._stream_lock:
            # Opening live view asks twice within milliseconds: the WebRTC
            # offer, then the HLS fallback. Arlo rejects the second
            # start_stream with 4006 ("Invalid camera activity state
            # change"), so hand out the URL the first one just got.
            # See STREAM_URL_REUSE_SECONDS. Cleared outright when Arlo ends
            # the session, because the URL dies with it and the remainder of
            # the window would hand out a corpse.
            if self._last_url and monotonic() - self._last_url[0] < STREAM_URL_REUSE_SECONDS:
                return self._with_transport(self._last_url[1])
            try:
                resp = await self.coordinator.call_with_session_retry(
                    "start_stream",
                    lambda: self.coordinator.client.start_stream(self._device.device_id),
                )
            except Exception:
                _LOGGER.exception("Failed to start stream for %s", self._device.device_id)
                return None
            url = resp.url.replace("rtsp://", "rtsps://", 1)
            self._last_url = (monotonic(), url)
            return self._with_transport(url)

    def _with_transport(self, url: str) -> str:
        """Apply the ``ffmpeg_stream`` option to a bare rtsps URL."""
        if _BARE_SOURCE.get():
            return url
        use_ffmpeg = self.coordinator.entry.options.get(CONF_FFMPEG_STREAM, DEFAULT_FFMPEG_STREAM)
        if use_ffmpeg and "go2rtc" in self.hass.config.components:
            return f"ffmpeg:{url}"
        return url

    def _stream_session_is_over(self) -> bool:
        """True when ``self.stream`` belongs to a session that has ended.

        An Arlo Stream is only ever valid for the session whose egress URL it
        was built around — unlike an ordinary RTSP camera, whose source is
        stable forever and which is the assumption HA's caching of
        ``self.stream`` is built on. Two signals, and both are needed:

        ``_attr_is_streaming`` says Arlo currently reports the stream running,
        which is authoritative while it is true and is what keeps a long live
        view from being torn down mid-watch.

        The reuse window says a URL was minted moments ago, so a Stream built
        around it belongs to the session being opened right now — Arlo has
        simply not published ``userStreamActive`` yet. Without this the pair of
        requests the frontend fires would see the first one's Stream as
        finished and rebuild it, undoing #33 one layer down.

        Neither holding means the Stream outlived its URL.
        """
        if self.stream is None or self._attr_is_streaming:
            return False
        if self._last_url is None:
            return True
        return monotonic() - self._last_url[0] >= STREAM_URL_REUSE_SECONDS

    async def async_create_stream(self) -> Stream | None:
        """Build HA's own Stream around the bare URL.

        This is the HLS path, and its worker is PyAV opening the URL directly —
        ``ffmpeg:`` means nothing to it, so an install that opted into the ffmpeg
        source for go2rtc used to have no working fallback: the player sat at
        0:00 while the worker retried a URL it could never parse. go2rtc still
        gets the prefixed source; only this caller is served the bare one.

        It is also the one door every viewer comes through, which makes it the
        place to refuse a Stream that has outlived its URL. 0.4.3 dropped the
        dead one on the streaming-to-idle transition, but that needs
        ``userStreamActive`` to have arrived first — and a live view that never
        starts never sends it, so the Stream built around the failed URL
        survived and HA handed that same object to every later viewer until a
        restart. One failed live view still broke every later one; only the
        symptom had moved.
        """
        if self._stream_session_is_over():
            finished = self.stream
            self.stream = None
            if finished is not None:
                _LOGGER.debug(
                    "Dropping the Stream of a finished session for %s",
                    self._device.device_id,
                )
                await finished.stop()

        token = _BARE_SOURCE.set(True)
        try:
            return await super().async_create_stream()
        finally:
            _BARE_SOURCE.reset(token)

    async def async_refresh_providers(self, *args: Any, **kwargs: Any) -> None:
        """Answer the WebRTC-provider probe without waking the camera.

        HA calls ``stream_source()`` here purely to read the URL scheme and
        decide whether go2rtc can handle this camera — at every entity add,
        so on every restart, reload and options change. Served for real it
        asks Arlo to start a user stream: the camera lights its LED and
        uploads a ~30 s clip nobody ever watches, three times per restart.
        """
        token = _PROBING.set(True)
        try:
            await super().async_refresh_providers(*args, **kwargs)
        finally:
            _PROBING.reset(token)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update streaming and motion detection state from coordinator."""
        state = self.coordinator.device_states.get(self._device.device_id)
        was_streaming = self._attr_is_streaming
        if state and state.activity_state:
            self._attr_is_streaming = state.activity_state in (
                "userStreamActive",
                "alertStreamActive",
            )
        else:
            self._attr_is_streaming = False

        # Stream just stopped — grab a final keyframe before the stream
        # worker tears down so the dashboard tile keeps a fresh image
        # (especially useful when the camera is then re-disarmed).
        if was_streaming and not self._attr_is_streaming and self.stream is not None:
            self.hass.async_create_task(self._cache_last_stream_frame())

        self._attr_motion_detection_enabled = (
            self.coordinator.mode_for_device(self._device.device_id) != "standby"
        )
        self.async_write_ha_state()

    async def _cache_last_stream_frame(self) -> None:
        """Keep the last frame, then drop the Stream Arlo has just killed.

        Arlo's egress URL dies with the stream, and HA's Stream object keeps
        the source it was built with: ``async_create_stream`` hands the old
        object back to every later viewer, whose worker then retries the dead
        URL on an ever-growing backoff. One failed live view used to poison
        every later one until a restart. Grab the final keyframe for the
        dashboard tile first, then tear it down so the next viewer builds a
        Stream around a fresh URL.
        """
        stream = self.stream
        if stream is None:
            return
        try:
            frame = await stream.async_get_image()
        except Exception:
            _LOGGER.debug("Failed to capture last stream frame", exc_info=True)
            frame = None
        if frame:
            self.coordinator.image_bytes[self._device.device_id] = frame
            _LOGGER.debug(
                "Cached %d bytes from stream end for %s", len(frame), self._device.device_id
            )
            await self.coordinator.archive_bytes(self._device.device_id, frame, "stream_thumb")
        if self.stream is stream:
            self.stream = None
            # The cached URL was minted for the session Arlo has just ended.
            # Leaving it would let a viewer arriving inside the reuse window
            # build a brand-new Stream around the same dead egress token —
            # the very failure dropping the Stream exists to prevent.
            self._last_url = None
            await stream.stop()
