"""Select platform for Eisenberg — security mode (Armed Away/Home/Standby).

Modes are scoped per location in Arlo's v3 automation API, so we expose one
security-mode select per location. Single-location accounts (the common case)
get exactly one select with the original unique_id, so upgrading does not
orphan the existing entity.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg.models import DeviceInfo, LocationState

from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)

# Order matches the Arlo app: most-restrictive first.
SECURITY_MODE_OPTIONS = ["armAway", "armHome", "standby"]

_LEGACY_UNIQUE_ID = "eisenberg_security_mode"


def _representative_device(
    coordinator: EisenbergCoordinator, location: LocationState
) -> DeviceInfo | None:
    """A device that lives in this location, to anchor the entity's device_info.

    Falls back to the first device so single-location accounts (and accounts
    where Arlo omitted gatewayDeviceIds) still attach to a device.
    """
    for device in coordinator.devices:
        if (device.parent_id or device.device_id) in location.gateway_device_ids:
            return device
    return coordinator.devices[0] if coordinator.devices else None


def build_mode_selects(coordinator: EisenbergCoordinator) -> list[SecurityModeSelect]:
    """One select per location. Single-location keeps the legacy unique_id."""
    single = len(coordinator.locations) == 1
    return [
        SecurityModeSelect(coordinator, location_id, single=single)
        for location_id in coordinator.locations
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one security mode select per location."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(build_mode_selects(coordinator))


class SecurityModeSelect(CoordinatorEntity[EisenbergCoordinator], SelectEntity):
    """Set one location's Arlo security mode via the v3 API."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:shield-home"
    _attr_options = SECURITY_MODE_OPTIONS

    def __init__(self, coordinator: EisenbergCoordinator, location_id: str, single: bool) -> None:
        super().__init__(coordinator)
        self._location_id = location_id
        location = coordinator.locations[location_id]
        if single:
            # Preserve the original entity on upgrade.
            self._attr_unique_id = _LEGACY_UNIQUE_ID
            self._attr_name = "Security mode"
        else:
            self._attr_unique_id = f"{_LEGACY_UNIQUE_ID}_{location_id}"
            label = location.location_name or location_id
            self._attr_name = f"Security mode ({label})"
        anchor = _representative_device(coordinator, location)
        if anchor is not None:
            self._attr_device_info = {
                "identifiers": {("eisenberg", anchor.device_id)},
            }
        self._attr_current_option = location.active_mode

    @callback
    def _handle_coordinator_update(self) -> None:
        location = self.coordinator.locations.get(self._location_id)
        self._attr_current_option = location.active_mode if location is not None else None
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_active_mode(self._location_id, option)
