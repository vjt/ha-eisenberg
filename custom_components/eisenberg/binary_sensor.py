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
        entities.append(BasestationConnectivity(coordinator, device))

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
        self._last_seen_event_key: str | None = None
        self._attr_name = f"{category} detected"
        self._attr_unique_id = f"{device.device_id}_{category.lower()}"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire only on a new event for our category.

        Every coordinator broadcast (siren/mode/motion from ANY device)
        re-runs this on ALL sensors, and motion_events keeps the last
        event per device forever. Guarding on is_on alone re-fired this
        sensor off its own stale event whenever some other camera updated.
        Track the last processed event so we act once per real event.
        """
        event = self.coordinator.motion_events.get(self._device.device_id)
        if not event or not event.obj_categories:
            return
        if self._category not in event.obj_categories:
            return

        event_key = event.feed_id or str(event.utc_created_date)
        if event_key == self._last_seen_event_key:
            return

        self._last_seen_event_key = event_key
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


class BasestationConnectivity(CoordinatorEntity[EisenbergCoordinator], BinarySensorEntity):
    """Connectivity binary sensor for the base station / camera gateway."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Base station connectivity"
    _attr_is_on: bool | None = None

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.device_id}_basestation_connectivity"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        state = self.coordinator.basestation_connection.get(self._device.device_id)
        if state is not None:
            self._attr_is_on = state == "available"
        self.async_write_ha_state()
