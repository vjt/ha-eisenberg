"""Camera platform for Eisenberg."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
        """Return the RTSP stream source URL."""
        try:
            resp = await self.coordinator.client.start_stream(self._device.device_id)
            return resp.url
        except Exception:
            _LOGGER.exception("Failed to start stream for %s", self._device.device_id)
            return None

    @property
    def is_streaming(self) -> bool:
        """Return whether the camera is streaming."""
        state = self.coordinator.device_states.get(self._device.device_id)
        if state and state.activity_state:
            return state.activity_state in (
                "userStreamActive",
                "alertStreamActive",
            )
        return False

    @property
    def motion_detection_enabled(self) -> bool:
        """Return whether motion detection is enabled."""
        return self.coordinator.active_mode != "standby"
