"""Light platform for Eisenberg — camera integrated spotlight."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from homeassistant.components.light import ATTR_BRIGHTNESS, LightEntity
from homeassistant.components.light.const import ColorMode
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
    """Set up Eisenberg lights."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(SpotlightLight(coordinator, device) for device in coordinator.devices)


class SpotlightLight(CoordinatorEntity[EisenbergCoordinator], LightEntity):
    """Arlo camera integrated spotlight.

    Arlo intensity is 0-100; HA brightness is 0-255 — convert on the
    boundary. Brightness is only sent when the user explicitly sets it
    so on/off toggles preserve the last picked intensity.
    """

    _attr_has_entity_name = True
    _attr_name = "Spotlight"
    _attr_icon = "mdi:spotlight"
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.device_id}_spotlight"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }
        self._attr_is_on = False
        self._attr_brightness: int | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        state = self.coordinator.spotlight_states.get(self._device.device_id)
        if state is not None:
            self._attr_is_on = state.enabled
            if state.intensity is not None:
                self._attr_brightness = round(state.intensity / 100 * 255)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        intensity: int | None = None
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None:
            intensity = round(int(brightness) / 255 * 100)
        await self.coordinator.call_with_session_retry(
            "set_spotlight_on",
            lambda: self.coordinator.client.set_spotlight(
                self._device.device_id, on=True, intensity=intensity
            ),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.call_with_session_retry(
            "set_spotlight_off",
            lambda: self.coordinator.client.set_spotlight(self._device.device_id, on=False),
        )
