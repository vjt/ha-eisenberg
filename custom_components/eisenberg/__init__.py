"""Eisenberg camera integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN as DOMAIN
from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)

type EisenbergConfigEntry = ConfigEntry[EisenbergCoordinator]

PLATFORMS = ["camera", "binary_sensor", "select", "sensor", "switch"]


class _ArloStreamRetryFilter(logging.Filter):
    """Drop the benign stream-worker retry error after a live view ends.

    HA's stream component caches the Stream object and its source URL,
    then retries the connection on its keepalive loop. Arlo's RTSPS URL
    embeds a one-shot egress token, so the retry always fails with
    "Invalid data found when processing input". The error is harmless —
    a fresh UI open builds a new stream from a new URL — but it spams
    the log. Pattern-match tightly so unrelated stream errors still log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not (
            "Invalid data found when processing input" in msg
            and "rtsps://" in msg
            and "vzmodulelive" in msg
        )


_STREAM_LOGGER_FILTER = _ArloStreamRetryFilter()


async def async_setup_entry(hass: HomeAssistant, entry: EisenbergConfigEntry) -> bool:
    """Set up Eisenberg from a config entry."""
    logging.getLogger("homeassistant.components.stream").addFilter(_STREAM_LOGGER_FILTER)

    coordinator = EisenbergCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EisenbergConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
        logging.getLogger("homeassistant.components.stream").removeFilter(_STREAM_LOGGER_FILTER)

    return unload_ok
