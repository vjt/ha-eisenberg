"""Select platform for Eisenberg — security mode (Away/Home/Standby + custom modes).

Modes are scoped per location in Arlo's v3 automation API, so we expose one
security-mode select per location, its unique_id scoped by location id.

Single-location accounts used to get the bare `eisenberg_security_mode`, which
a second Arlo account (a second config entry) then collided with, losing its
select (#43). That entity is migrated in the registry to the scoped id, so its
entity_id, history and automations survive the upgrade.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg.models import DeviceInfo, LocationState

from .const import DOMAIN
from .coordinator import EisenbergCoordinator, resolve_location_for_device

_LOGGER = logging.getLogger(__name__)

_LEGACY_UNIQUE_ID = "eisenberg_security_mode"


def _unique_id(location_id: str) -> str:
    return f"{_LEGACY_UNIQUE_ID}_{location_id}"


def migrate_legacy_unique_id(
    registry: er.EntityRegistry, entry_id: str, location_ids: list[str]
) -> None:
    """Move this entry's legacy-id select to its location-scoped id (#43).

    Only a single-location entry ever got the legacy id, so that is the only
    case with a location to move it to. Another entry's legacy entity, or a
    scoped id already in the registry, is left alone.
    """
    if len(location_ids) != 1:
        return
    entity_id = registry.async_get_entity_id("select", DOMAIN, _LEGACY_UNIQUE_ID)
    if entity_id is None:
        return
    registered = registry.async_get(entity_id)
    if registered is None or registered.config_entry_id != entry_id:
        return
    scoped = _unique_id(location_ids[0])
    if registry.async_get_entity_id("select", DOMAIN, scoped) is not None:
        return
    registry.async_update_entity(entity_id, new_unique_id=scoped)
    _LOGGER.info("Migrated %s to unique_id %s", entity_id, scoped)


def _representative_device(
    coordinator: EisenbergCoordinator, location: LocationState
) -> DeviceInfo | None:
    """A device that lives in this location, to anchor the entity's device_info.

    Falls back to the first device so single-location accounts (and accounts
    where Arlo omitted gatewayDeviceIds) still attach to a device.
    """
    for device in coordinator.devices:
        if resolve_location_for_device(device, [location]) is not None:
            return device
    return coordinator.devices[0] if coordinator.devices else None


def build_mode_selects(coordinator: EisenbergCoordinator) -> list[SecurityModeSelect]:
    """One select per location; a single location keeps the plain name."""
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
    migrate_legacy_unique_id(er.async_get(hass), entry.entry_id, list(coordinator.locations))
    async_add_entities(build_mode_selects(coordinator))


class SecurityModeSelect(CoordinatorEntity[EisenbergCoordinator], SelectEntity):
    """Set one location's Arlo security mode via the v3 API."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:shield-home"

    def __init__(self, coordinator: EisenbergCoordinator, location_id: str, single: bool) -> None:
        super().__init__(coordinator)
        self._location_id = location_id
        location = coordinator.locations[location_id]
        self._attr_unique_id = _unique_id(location_id)
        if single:
            self._attr_name = "Security mode"
        else:
            label = location.location_name or location_id
            self._attr_name = f"Security mode ({label})"
        anchor = _representative_device(coordinator, location)
        if anchor is not None:
            self._attr_device_info = {
                "identifiers": {("eisenberg", anchor.device_id)},
            }
        self._attr_options = location.mode_options
        self._attr_current_option = location.mode_label

    @callback
    def _handle_coordinator_update(self) -> None:
        # Options follow the location's custom modes, which the Arlo app can
        # add, rename or delete at any time (#39).
        location = self.coordinator.locations.get(self._location_id)
        if location is not None:
            self._attr_options = location.mode_options
        self._attr_current_option = location.mode_label if location is not None else None
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_active_mode(self._location_id, option)
