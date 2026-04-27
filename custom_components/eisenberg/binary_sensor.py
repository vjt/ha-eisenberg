"""Binary sensor platform for Eisenberg.

Motion detected: from MQTT motionDetected property (resets via MQTT).
Person/Vehicle/Animal: from AI classification in feed/live events
(auto-resets after configurable timeout since MQTT only resets generic motion).
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg import DeviceInfo

from .const import CONF_DETECTION_TIMEOUT, DEFAULT_DETECTION_TIMEOUT
from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Eisenberg binary sensors."""
    coordinator: EisenbergCoordinator = entry.runtime_data

    entities: list[BinarySensorEntity] = []
    for device in coordinator.devices:
        entities.append(MotionSensor(coordinator, device))
        entities.append(DetectionSensor(coordinator, device, "Person", entry))
        entities.append(DetectionSensor(coordinator, device, "Vehicle", entry))
        entities.append(DetectionSensor(coordinator, device, "Animal", entry))

    async_add_entities(entities)


class MotionSensor(CoordinatorEntity[EisenbergCoordinator], BinarySensorEntity):
    """Motion detected binary sensor — directly from MQTT motionDetected."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_name = "Motion"
    _attr_is_on: bool | None = False

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.device_id}_motion"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update is_on from coordinator state.

        Only overwrite when MQTT actually reported a motion value — most
        camera state updates (snapshots, mode changes) do not include
        motionDetected and would otherwise reset the sensor to unknown.
        """
        state = self.coordinator.device_states.get(self._device.device_id)
        if state is not None and state.motion_detected is not None:
            self._attr_is_on = state.motion_detected
        self.async_write_ha_state()


class DetectionSensor(CoordinatorEntity[EisenbergCoordinator], BinarySensorEntity):
    """AI detection binary sensor (person/vehicle/animal).

    Turns on when the AI classification matches, auto-resets after
    a configurable timeout since MQTT only sends motionDetected=false
    for generic motion, not per-category.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_is_on: bool | None = False

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
        category: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._category = category
        self._entry = entry
        self._reset_task: asyncio.Task[None] | None = None
        self._attr_name = f"{category} detected"
        self._attr_unique_id = f"{device.device_id}_{category.lower()}"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Check if latest motion event matches our category."""
        event = self.coordinator.motion_events.get(self._device.device_id)
        if (
            event
            and event.obj_categories
            and self._category in event.obj_categories
            and not self._attr_is_on
        ):
            self._attr_is_on = True
            self._schedule_reset()
            self.async_write_ha_state()

    def _schedule_reset(self) -> None:
        """Schedule auto-reset after timeout."""
        if self._reset_task:
            self._reset_task.cancel()

        timeout = self._entry.options.get(CONF_DETECTION_TIMEOUT, DEFAULT_DETECTION_TIMEOUT)

        async def _reset() -> None:
            await asyncio.sleep(timeout)
            self._attr_is_on = False
            self.async_write_ha_state()

        self._reset_task = self.hass.async_create_task(_reset())
