"""Switch platform for Eisenberg — siren control."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
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
    """Set up Eisenberg switches."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(SirenSwitch(coordinator, device) for device in coordinator.cameras)


class SirenSwitch(CoordinatorEntity[EisenbergCoordinator], SwitchEntity):
    """Siren on/off switch."""

    _attr_has_entity_name = True
    _attr_name = "Siren"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:alarm-light"
    _attr_is_on: bool | None = False

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.device_id}_siren"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update siren state from coordinator."""
        state = self.coordinator.siren_states.get(self._device.device_id)
        if state:
            self._attr_is_on = state.is_on
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn siren on."""
        await self.coordinator.call_with_session_retry(
            "set_siren_on",
            lambda: self.coordinator.client.set_siren(self._device.device_id, on=True),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn siren off."""
        await self.coordinator.call_with_session_retry(
            "set_siren_off",
            lambda: self.coordinator.client.set_siren(self._device.device_id, on=False),
        )
