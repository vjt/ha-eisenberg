"""Button platform for Eisenberg — manual full-frame snapshot (#8).

One SnapshotButton per camera. Pressing it asks Arlo for a fresh
full-frame snapshot; the image arrives later over MQTT and refreshes the
camera tile, exactly as motion-triggered snapshots do. The standby-guard
and session-retry live on the coordinator so this button and the
`eisenberg.snapshot` camera service share one code path.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
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
    """Set up Eisenberg snapshot buttons."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(SnapshotButton(coordinator, device) for device in coordinator.devices)


class SnapshotButton(CoordinatorEntity[EisenbergCoordinator], ButtonEntity):
    """Trigger a manual full-frame snapshot on a camera."""

    _attr_has_entity_name = True
    _attr_name = "Snapshot"
    _attr_icon = "mdi:camera-iris"

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.device_id}_snapshot"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    async def async_press(self) -> None:
        await self.coordinator.request_snapshot(self._device.device_id)
