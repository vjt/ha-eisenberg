"""Camera platform for Eisenberg."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg import DeviceInfo

from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Eisenberg cameras."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(EisenbergCamera(coordinator, device) for device in coordinator.devices)


class EisenbergCamera(CoordinatorEntity[EisenbergCoordinator], Camera):
    """Arlo camera entity with snapshot and RTSP stream support."""

    _attr_has_entity_name = True
    _attr_name = None  # Use device name
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_is_streaming: bool = False
    _attr_motion_detection_enabled: bool = True

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
        """Return the latest camera image."""
        # Try latest thumbnail from motion event first
        url = self.coordinator.latest_thumbnails.get(self._device.device_id)
        if not url:
            # Try latest snapshot
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
        if state and state.activity_state:
            self._attr_is_streaming = state.activity_state in (
                "userStreamActive",
                "alertStreamActive",
            )
        else:
            self._attr_is_streaming = False

        self._attr_motion_detection_enabled = self.coordinator.active_mode != "standby"
        self.async_write_ha_state()
