"""Camera platform for Eisenberg."""

from __future__ import annotations

import logging
from typing import ClassVar

import aiohttp
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg import DeviceInfo

from .coordinator import EisenbergCoordinator

SERVICE_SNAPSHOT = "snapshot"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Eisenberg cameras."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(EisenbergCamera(coordinator, device) for device in coordinator.devices)

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
        1. A live keyframe from HA's stream worker if a stream object
           still exists — this gives a fresh image while or just after
           live view, and is the only way to refresh the tile while the
           camera is disarmed (Arlo refuses on-demand snapshots then).
        2. Bytes cached by the coordinator from MQTT-delivered URLs.
        3. Refetch from the most recent snapshot/thumbnail URL.
        """
        if self.stream is not None:
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

        The image arrives later via MQTT (fullFrameSnapshotAvailable) and
        the coordinator caches/archives it. Arlo refuses with error 4006
        when the camera is in standby — surface that as HomeAssistantError
        so the service call fails loudly instead of silently no-oping.
        """
        if self.coordinator.active_mode == "standby":
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(
                "Cannot snapshot while disarmed — Arlo refuses cloud snapshots in standby"
            )
        try:
            await self.coordinator.client.request_snapshot(self._device.device_id)
        except Exception as err:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(f"Snapshot request failed: {err}") from err

    async def stream_source(self) -> str | None:
        """Return the RTSP stream source URL.

        Arlo serves the stream on port 443 with TLS but advertises it as
        plain rtsp://; ffmpeg fails to read the bytes because they're
        actually TLS-wrapped. Rewrite the scheme to rtsps:// (matches
        pyaarlo's behaviour) so HA's stream worker negotiates TLS.
        """
        try:
            resp = await self.coordinator.client.start_stream(self._device.device_id)
        except Exception:
            _LOGGER.exception("Failed to start stream for %s", self._device.device_id)
            return None
        return resp.url.replace("rtsp://", "rtsps://", 1)

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

        self._attr_motion_detection_enabled = self.coordinator.active_mode != "standby"
        self.async_write_ha_state()

    async def _cache_last_stream_frame(self) -> None:
        # Don't tear down the Stream — HA reuses it for follow-up clients
        # (more frame requests, websocket HLS endpoint negotiation), and
        # forcing self.stream = None would race with those. The trade-off
        # is the occasional "Invalid data" log when HA's keepalive retries
        # with an expired Arlo egress token; that's noise, not a fault.
        if self.stream is None:
            return
        try:
            frame = await self.stream.async_get_image()
        except Exception:
            _LOGGER.debug("Failed to capture last stream frame", exc_info=True)
            return
        if frame:
            self.coordinator.image_bytes[self._device.device_id] = frame
            _LOGGER.debug(
                "Cached %d bytes from stream end for %s", len(frame), self._device.device_id
            )
            await self.coordinator.archive_bytes(self._device.device_id, frame, "stream_thumb")
