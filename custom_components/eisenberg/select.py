"""Select platform for Eisenberg — security mode (Armed Away/Home/Standby)."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)

# Order matches the Arlo app: most-restrictive first.
SECURITY_MODE_OPTIONS = ["armAway", "armHome", "standby"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the security mode select entity (one per account)."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities([SecurityModeSelect(coordinator)])


class SecurityModeSelect(CoordinatorEntity[EisenbergCoordinator], SelectEntity):
    """Set the Arlo security mode at the location level via the v3 API."""

    _attr_has_entity_name = True
    _attr_name = "Security mode"
    _attr_icon = "mdi:shield-home"
    _attr_options = SECURITY_MODE_OPTIONS

    def __init__(self, coordinator: EisenbergCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "eisenberg_security_mode"
        if coordinator.devices:
            self._attr_device_info = {
                "identifiers": {("eisenberg", coordinator.devices[0].device_id)},
            }
        self._attr_current_option = coordinator.active_mode

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_current_option = self.coordinator.active_mode
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_active_mode(option)
