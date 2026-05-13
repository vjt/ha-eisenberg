"""Eisenberg camera integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN as DOMAIN
from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)

type EisenbergConfigEntry = ConfigEntry[EisenbergCoordinator]

PLATFORMS = ["camera", "binary_sensor", "light", "select", "sensor", "switch"]


class _ArloStreamRetryFilter(logging.Filter):
    """Drop the benign stream-worker retry error after a live view ends.

    HA's stream component caches the Stream object and its source URL,
    then retries the connection on its keepalive loop. Arlo's RTSPS URL
    embeds a one-shot egress token, so any retry against it fails — the
    underlying ffmpeg surfaces variants like "Invalid data found when
    processing input" or "Error demuxing stream while finding first
    packet (I/O error)". Match on the URL signature instead of the error
    text so all variants are silenced while unrelated stream errors
    (different cameras, different protocols) keep their voice.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        if not record.name.startswith("homeassistant.components.stream"):
            return True
        msg = record.getMessage()
        # Must be (a) the stream-worker retry surface, and (b) hitting
        # an Arlo / Wowza URL. Genuine errors that match only one half
        # still log.
        return not ("Error from stream worker" in msg and "vzmodulelive" in msg)


_STREAM_LOGGER_FILTER = _ArloStreamRetryFilter()


async def async_setup_entry(hass: HomeAssistant, entry: EisenbergConfigEntry) -> bool:
    """Set up Eisenberg from a config entry."""
    # Attach the filter to the root logger's handlers — HA's stream
    # worker logs under per-camera child loggers
    # (homeassistant.components.stream.stream.camera.<entity>), and
    # filters set on a parent logger don't run for records that
    # originated below it. Handler-level filters do.
    for handler in logging.getLogger().handlers:
        handler.addFilter(_STREAM_LOGGER_FILTER)

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
        for handler in logging.getLogger().handlers:
            handler.removeFilter(_STREAM_LOGGER_FILTER)

    return unload_ok
