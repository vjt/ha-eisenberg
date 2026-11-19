"""Event platform for Eisenberg: a doorbell event per video doorbell.

Home Assistant's event entity is the native shape for a button press — it
has no "off" to fake with a timer, and automations trigger on it directly.
The "Doorbell pressed" binary sensor exists alongside it for setups that
prefer a state trigger.
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg import DeviceInfo

from .const import DOORBELL_EVENT_RING
from .coordinator import EisenbergCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Eisenberg event entities."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(DoorbellEvent(coordinator, d) for d in coordinator.doorbells)


class DoorbellEvent(CoordinatorEntity[EisenbergCoordinator], EventEntity):
    """Fires a `ring` event each time the doorbell's button is pressed."""

    _attr_has_entity_name = True
    _attr_name = "Doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_event_types = [DOORBELL_EVENT_RING]
        # Presses before this entity existed are history, not a ring.
        self._last_seen_presses = coordinator.doorbell_presses.get(device.device_id, 0)
        self._attr_unique_id = f"{device.device_id}_doorbell"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire once per new press; ignore every other broadcast."""
        presses = self.coordinator.doorbell_presses.get(self._device.device_id, 0)
        if presses == self._last_seen_presses:
            return
        self._last_seen_presses = presses
        self._trigger_event(DOORBELL_EVENT_RING)
        self.async_write_ha_state()
