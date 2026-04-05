"""Sensor platform for Eisenberg — battery and signal strength."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg import DeviceInfo

from .coordinator import EisenbergCoordinator

if TYPE_CHECKING:
    from datetime import date, datetime
    from decimal import Decimal


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Eisenberg sensors."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for device in coordinator.devices:
        entities.append(BatterySensor(coordinator, device))
        entities.append(SignalStrengthSensor(coordinator, device))
    async_add_entities(entities)


class BatterySensor(CoordinatorEntity[EisenbergCoordinator], SensorEntity):
    """Battery level sensor."""

    _attr_has_entity_name = True
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_value: StateType | date | datetime | Decimal = None

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.device_id}_battery"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }
        # Seed from initial device info
        props = device.properties or {}
        battery = props.get("batteryLevel")
        if isinstance(battery, int):
            self._attr_native_value = battery

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update battery level from coordinator state."""
        state = self.coordinator.device_states.get(self._device.device_id)
        if state and state.battery_level is not None:
            self._attr_native_value = state.battery_level
        self.async_write_ha_state()


class SignalStrengthSensor(CoordinatorEntity[EisenbergCoordinator], SensorEntity):
    """WiFi signal strength sensor."""

    _attr_has_entity_name = True
    _attr_name = "Signal strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_registry_enabled_default = False
    _attr_native_value: StateType | date | datetime | Decimal = None

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.device_id}_signal"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update signal strength from coordinator state."""
        state = self.coordinator.device_states.get(self._device.device_id)
        if state and state.signal_strength is not None:
            self._attr_native_value = state.signal_strength
        self.async_write_ha_state()
