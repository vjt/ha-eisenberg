"""Event-driven coordinator for Eisenberg.

Unlike a typical DataUpdateCoordinator that polls, this coordinator
listens to MQTT events and updates entity state in real-time. The
periodic update is only used for health checks (token refresh, device
list sync).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from eisenberg import (
    AuthenticationError,
    DeviceInfo,
    EisenbergClient,
    MQTTEventStream,
)
from eisenberg.models import (
    ActiveMode,
    DeviceState,
    MediaUpload,
    ModeChangeEvent,
    MotionEvent,
    SirenState,
    SnapshotAvailable,
)

from .const import (
    CONF_DEVICE_ID,
    CONF_MEDIA_DIR,
    CONF_TRUST_COOKIE,
    DOMAIN,
    EVENT_MEDIA,
)

_LOGGER = logging.getLogger(__name__)

# Health check interval (token refresh, device sync)
HEALTH_CHECK_INTERVAL = timedelta(minutes=30)


class EisenbergCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Eisenberg coordinator — MQTT event-driven."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=HEALTH_CHECK_INTERVAL,
        )
        self.entry = entry
        self.client = EisenbergClient(
            email=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            device_id=entry.data[CONF_DEVICE_ID],
        )
        self._mqtt: MQTTEventStream | None = None
        self._devices: list[DeviceInfo] = []
        self._http_session: aiohttp.ClientSession | None = None

        # Entity state — updated by MQTT handlers
        self.device_states: dict[str, DeviceState] = {}
        self.siren_states: dict[str, SirenState] = {}
        self.active_mode: str | None = None
        self.latest_snapshots: dict[str, str] = {}  # device_id -> URL
        self.latest_thumbnails: dict[str, str] = {}  # device_id -> URL or path
        self.motion_events: dict[str, MotionEvent] = {}  # device_id -> last event

    @property
    def devices(self) -> list[DeviceInfo]:
        return self._devices

    @property
    def media_dir(self) -> str:
        """Configured media directory key, or empty string for disabled."""
        return self.entry.options.get(CONF_MEDIA_DIR, "")

    @property
    def media_path(self) -> Path | None:
        """Resolved media storage path, or None if disabled."""
        key = self.media_dir
        if not key:
            return None
        path_str = self.hass.config.media_dirs.get(key)
        if not path_str:
            return None
        return Path(path_str) / "eisenberg"

    async def async_setup(self) -> None:
        """Initialize client and MQTT on first refresh."""

        from yarl import URL

        cookie_jar = aiohttp.CookieJar(unsafe=True)

        # Restore only the browser trust cookie — skip transient cookies
        # (__cf_bm, AWSALB, JSESSIONID) which expire quickly and cause issues
        saved_cookies: list[dict[str, str]] = self.entry.data.get(CONF_TRUST_COOKIE, [])
        for cookie_data in saved_cookies:
            if not cookie_data["name"].startswith("browser_trust_"):
                continue
            domain = cookie_data.get("domain", "ocapi-app.arlo.com")
            if domain.startswith("."):
                domain = domain[1:]
            path = cookie_data.get("path", "/")
            # Value stays URL-encoded (e.g. %3D not =) to avoid http.cookies quoting
            cookie_jar.update_cookies(
                {cookie_data["name"]: cookie_data["value"]},
                URL(f"https://{domain}{path}"),
            )

        _LOGGER.info(
            "Restored %d cookies, trust cookie present: %s",
            len(saved_cookies),
            any(c["name"].startswith("browser_trust_") for c in saved_cookies),
        )

        self._http_session = aiohttp.ClientSession(cookie_jar=cookie_jar)
        self.client.set_http_session(self._http_session)

        try:
            await self.client.login()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        self._devices = await self.client.get_devices()

        # Start MQTT
        if self.client.mqtt_url and self.client.user_id and self.client.token:
            self._mqtt = MQTTEventStream(
                mqtt_url=self.client.mqtt_url,
                user_id=self.client.user_id,
                token=self.client.token,
                x_cloud_id=self.client.x_cloud_id,
                http_session=self._http_session,
            )
            self._register_mqtt_handlers()
            await self._mqtt.connect()

        # Request initial snapshots for all cameras
        for device in self._devices:
            try:
                await self.client.request_snapshot(device.device_id)
                _LOGGER.debug("Requested initial snapshot for %s", device.device_id)
            except Exception:
                _LOGGER.debug("Could not request snapshot for %s", device.device_id)

    def _register_mqtt_handlers(self) -> None:
        """Register MQTT topic handlers."""
        if self._mqtt is None:
            return

        # Camera state updates
        self._mqtt.on("d/+/out/cameras/+/is", self._handle_camera_state)

        # Snapshot available
        self._mqtt.on(
            "d/+/out/cameras/+/fullFrameSnapshotAvailable",
            self._handle_snapshot,
        )

        # Siren state
        self._mqtt.on("d/+/out/siren/+/is", self._handle_siren)

        # Feed notifications (motion events, mode changes)
        self._mqtt.on("u/+/in/feed/live", self._handle_feed)

        # Media uploads
        self._mqtt.on("u/+/in/library/add", self._handle_media_upload)

        # Mode changes
        self._mqtt.on("u/+/in/automation/activeMode/is", self._handle_active_mode)

        # Connectivity
        self._mqtt.on(
            "d/+/out/basestation/connectivity/is",
            self._handle_connectivity,
        )

        # Reconnect handler
        self._mqtt.on_disconnect(self._handle_mqtt_disconnect)

    async def _handle_camera_state(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle camera device state updates."""
        # Extract device_id from topic: d/{xCloudId}/out/cameras/{deviceId}/is
        parts = topic.split("/")
        if len(parts) < 5:
            return
        device_id = parts[4]

        properties = payload.get("properties") or payload.get("states", {})
        try:
            state = DeviceState.model_validate(properties)
            self.device_states[device_id] = state
            _LOGGER.debug(
                "Camera %s state: motion=%s activity=%s",
                device_id,
                state.motion_detected,
                state.activity_state,
            )
            self.async_set_updated_data(self.data or {})
        except Exception:
            _LOGGER.warning(
                "Failed to parse camera state for %s: %s",
                device_id,
                json.dumps(payload)[:500],
            )

    async def _handle_snapshot(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle snapshot available notification."""
        parts = topic.split("/")
        if len(parts) < 5:
            return
        device_id = parts[4]

        properties = payload.get("properties", {})
        try:
            snap = SnapshotAvailable.model_validate(properties)
            self.latest_snapshots[device_id] = snap.presigned_url
            _LOGGER.debug("Snapshot available for %s", device_id)

            # Archive if configured
            await self._archive_media(device_id, snap.presigned_url, "snapshot", "jpg")

            self.async_set_updated_data(self.data or {})
        except Exception:
            _LOGGER.warning(
                "Failed to parse snapshot for %s: %s",
                device_id,
                json.dumps(payload)[:500],
            )

    async def _handle_siren(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle siren state updates."""
        parts = topic.split("/")
        if len(parts) < 5:
            return
        device_id = parts[4]

        properties = payload.get("properties", {})
        try:
            state = SirenState.model_validate(properties)
            self.siren_states[device_id] = state
            _LOGGER.debug("Siren %s: %s", device_id, state.siren_state)
            self.async_set_updated_data(self.data or {})
        except Exception:
            _LOGGER.warning(
                "Failed to parse siren for %s: %s",
                device_id,
                json.dumps(payload)[:500],
            )

    async def _handle_feed(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle feed notifications (motion events, mode changes)."""
        feed_type = payload.get("type")

        if feed_type == "motion":
            try:
                event = MotionEvent.model_validate(payload)
                self.motion_events[event.device_id] = event
                _LOGGER.debug(
                    "Motion event: device=%s category=%s",
                    event.device_id,
                    event.obj_category,
                )

                # Archive media
                if event.content_url:
                    await self._archive_media(
                        event.device_id,
                        event.content_url,
                        f"motion_{(event.obj_category or 'unknown').lower()}",
                        "mp4",
                    )
                if event.thumbnail_url:
                    await self._archive_media(
                        event.device_id,
                        event.thumbnail_url,
                        f"motion_{(event.obj_category or 'unknown').lower()}_thumb",
                        "jpg",
                    )
                    self.latest_thumbnails[event.device_id] = event.thumbnail_url

                # Fire HA event
                self.hass.bus.async_fire(
                    EVENT_MEDIA,
                    {
                        "device_id": event.device_id,
                        "type": "motion",
                        "category": event.obj_category,
                        "categories": event.obj_categories,
                        "content_url": event.content_url,
                        "thumbnail_url": event.thumbnail_url,
                        "duration": event.duration,
                        "timestamp": event.utc_created_date,
                    },
                )

                self.async_set_updated_data(self.data or {})
            except Exception:
                _LOGGER.warning(
                    "Failed to parse motion event: %s",
                    json.dumps(payload)[:500],
                )

        elif feed_type == "modeChange":
            try:
                event = ModeChangeEvent.model_validate(payload)
                self.active_mode = event.active_mode
                _LOGGER.debug("Mode change: %s", event.active_mode)
                self.async_set_updated_data(self.data or {})
            except Exception:
                _LOGGER.warning(
                    "Failed to parse mode change: %s",
                    json.dumps(payload)[:500],
                )
        else:
            _LOGGER.info(
                "Unknown feed type '%s': %s",
                feed_type,
                json.dumps(payload)[:500],
            )

    async def _handle_media_upload(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle media upload notifications."""
        try:
            upload = MediaUpload.model_validate(payload)
            _LOGGER.debug(
                "Media upload for %s: stopped=%s",
                upload.device_id,
                upload.recording_stopped,
            )
        except Exception:
            _LOGGER.warning(
                "Failed to parse media upload: %s",
                json.dumps(payload)[:500],
            )

    async def _handle_active_mode(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle automation active mode updates."""
        properties = payload.get("properties", {})
        try:
            mode = ActiveMode.model_validate(properties)
            self.active_mode = mode.properties.mode
            _LOGGER.debug("Active mode: %s", self.active_mode)
            self.async_set_updated_data(self.data or {})
        except Exception:
            _LOGGER.warning(
                "Failed to parse active mode: %s",
                json.dumps(payload)[:500],
            )

    async def _handle_connectivity(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle connectivity updates."""
        _LOGGER.debug("Connectivity update: %s", json.dumps(payload)[:200])

    async def _handle_mqtt_disconnect(self) -> None:
        """Handle MQTT disconnect — attempt reconnect."""
        _LOGGER.warning("MQTT disconnected, will reconnect on next refresh")
        self._mqtt = None

    async def _archive_media(
        self,
        device_id: str,
        url: str,
        media_type: str,
        ext: str,
    ) -> None:
        """Download and archive media to configured storage."""
        media_path = self.media_path
        if media_path is None:
            return

        from datetime import UTC, datetime

        now = datetime.now(UTC)
        date_dir = media_path / now.strftime("%Y-%m-%d")

        def _ensure_dir() -> None:
            date_dir.mkdir(parents=True, exist_ok=True)

        await self.hass.async_add_executor_job(_ensure_dir)

        timestamp = int(now.timestamp())
        filename = f"{timestamp}_{media_type}.{ext}"
        filepath = date_dir / filename

        try:
            if self._http_session is None:
                return
            async with self._http_session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.read()

                    def _write() -> None:
                        filepath.write_bytes(content)

                    await self.hass.async_add_executor_job(_write)
                    _LOGGER.debug("Archived %s to %s", media_type, filepath)
                else:
                    _LOGGER.warning("Failed to download %s: HTTP %d", url, resp.status)
        except Exception:
            _LOGGER.exception("Error archiving media %s", url)

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic health check: token refresh, MQTT reconnect."""
        # Token refresh
        if self.client.token_needs_refresh():
            _LOGGER.info("Refreshing auth token")
            try:
                await self.client.login()
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed(str(err)) from err

        # MQTT reconnect
        if self._mqtt is None and self.client.mqtt_url:
            _LOGGER.info("Reconnecting MQTT")
            try:
                if self.client.user_id and self.client.token and self._http_session:
                    self._mqtt = MQTTEventStream(
                        mqtt_url=self.client.mqtt_url,
                        user_id=self.client.user_id,
                        token=self.client.token,
                        x_cloud_id=self.client.x_cloud_id,
                        http_session=self._http_session,
                    )
                    self._register_mqtt_handlers()
                    await self._mqtt.connect()
            except Exception:
                _LOGGER.exception("MQTT reconnect failed")
                self._mqtt = None

        return {}

    async def async_shutdown(self) -> None:
        """Clean shutdown."""
        if self._mqtt:
            await self._mqtt.disconnect()
            self._mqtt = None
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
