"""Sensor platform for Eisenberg — battery and signal strength."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg import DeviceInfo

from .coordinator import EisenbergCoordinator


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

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.device_states.get(self._device.device_id)
        if state and state.battery_level is not None:
            return state.battery_level
        # Fallback to initial device info
        props = self._device.properties or {}
        return props.get("batteryLevel")


class SignalStrengthSensor(CoordinatorEntity[EisenbergCoordinator], SensorEntity):
    """WiFi signal strength sensor."""

    _attr_has_entity_name = True
    _attr_name = "Signal strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_registry_enabled_default = False

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

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.device_states.get(self._device.device_id)
        if state and state.signal_strength is not None:
            return state.signal_strength
        return None
