"""Eisenberg camera integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN as DOMAIN
from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)

type EisenbergConfigEntry = ConfigEntry[EisenbergCoordinator]

PLATFORMS = ["camera", "binary_sensor", "sensor", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: EisenbergConfigEntry) -> bool:
    """Set up Eisenberg from a config entry."""
    coordinator = EisenbergCoordinator(hass, entry)
    await coordinator._async_setup()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EisenbergConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()

    return unload_ok
