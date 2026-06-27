"""Event-driven coordinator for Eisenberg.

Unlike a typical DataUpdateCoordinator that polls, this coordinator
listens to MQTT events and updates entity state in real-time. The
periodic update is only used for health checks (token refresh, device
list sync).
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

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
    MfaRequired,
    MQTTEventStream,
    RateLimitedError,
    SessionExpiredError,
)
from eisenberg.models import (
    ActiveMode,
    BasestationState,
    DeviceState,
    LocationState,
    MediaUpload,
    ModeChangeEvent,
    MotionEvent,
    SirenState,
    SnapshotAvailable,
    SpotlightState,
)

from .const import (
    CONF_DEVICE_ID,
    CONF_MEDIA_DIR,
    CONF_MEDIA_RETENTION_DAYS,
    CONF_TRUST_COOKIE,
    DEFAULT_MEDIA_RETENTION_DAYS,
    DOMAIN,
    EVENT_MEDIA,
)

_LOGGER = logging.getLogger(__name__)

# Health check interval (token refresh, device sync)
HEALTH_CHECK_INTERVAL = timedelta(minutes=30)


def resolve_location_for_device(
    device: DeviceInfo, locations: Iterable[LocationState]
) -> LocationState | None:
    """Return the location whose gateway list contains this device's gateway.

    A device's gateway is its base station (`parentId`), or its own `deviceId`
    when it is base-less (e.g. an Essential XL, which is its own base). Returns
    None when no location claims the gateway.
    """
    gateway = device.parent_id or device.device_id
    for location in locations:
        if gateway in location.gateway_device_ids:
            return location
    return None


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

        # Entity state — updated by MQTT handlers.
        self.device_states: dict[str, DeviceState] = {}
        self.siren_states: dict[str, SirenState] = {}
        self.spotlight_states: dict[str, SpotlightState] = {}
        # Mode is scoped per location in Arlo's v3 automation API. We keep one
        # LocationState per location (identity + gateway membership + current
        # mode + revision), learned at startup, so each location's select is
        # responsive immediately and its PUT revision counter stays in sync.
        # A device maps to a location via its gateway (parentId, or its own id
        # when base-less); see resolve_location_for_device / location_for_device.
        self.locations: dict[str, LocationState] = {}
        self.latest_snapshots: dict[str, str] = {}  # device_id -> URL
        self.latest_thumbnails: dict[str, str] = {}  # device_id -> URL or path
        # Most recent image bytes per device. Cached so the dashboard tile
        # has something to show even after Arlo's presigned URL has
        # expired or while the camera is disarmed (no live snapshots
        # possible from the cloud in that state).
        self.image_bytes: dict[str, bytes] = {}
        self.motion_events: dict[str, MotionEvent] = {}  # device_id -> last event
        # gateway_id -> last connectionState ("available" / "unavailable" / ...)
        self.basestation_connection: dict[str, str] = {}

    @property
    def devices(self) -> list[DeviceInfo]:
        return self._devices

    def _mqtt_extra_topics(self) -> list[str]:
        """Union of every device's declared allowedMqttTopics, order-stable.

        These are the authoritative per-device topic filters. Doorbells and
        base-less cameras publish motion/battery/signal on roots the broad
        `d/{xCloudId}/out/#` wildcard can miss, so we hand them to the
        subscriber explicitly. Empty when no device declares any.
        """
        topics: list[str] = []
        for device in self._devices:
            for topic in device.allowed_mqtt_topics:
                if topic not in topics:
                    topics.append(topic)
        return topics

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

    async def _login_silent(self) -> None:
        """Login using trust cookie. Any auth failure → ConfigEntryAuthFailed.

        Coordinator NEVER triggers push at startup — push approval is
        user-driven via the reauth flow. Each finishAuth call costs a
        rate-limit token; an unattended retry loop can lock the user out.
        """
        _LOGGER.info("Silent login starting (trust cookie based)")
        await self.client.login()
        _LOGGER.info("Silent login complete; mqtt_url=%s", self.client.mqtt_url)
        await self._save_cookies()

    async def _seed_image_cache_from_disk(self) -> None:
        """Load the newest archived JPEG per device into image_bytes.

        Lets the dashboard tile survive HA restarts and long disarmed
        gaps. Scans the media archive (configured during setup) for the
        most recent `*_thumb.jpg` or `*_snapshot.jpg` matching each
        device id and loads its bytes synchronously off the executor.
        """
        media_path = self.media_path
        if media_path is None or not self._devices:
            return

        device_ids = {d.device_id for d in self._devices}

        def _scan() -> dict[str, bytes]:
            if not media_path.exists():
                return {}
            newest: dict[str, tuple[float, Path]] = {}
            for path in media_path.rglob("*.jpg"):
                # filename: {ts}_{device_id}_{type}.jpg
                parts = path.stem.split("_", 2)
                if len(parts) < 3:
                    continue
                device_id = parts[1]
                if device_id not in device_ids:
                    continue
                mtime = path.stat().st_mtime
                if device_id not in newest or newest[device_id][0] < mtime:
                    newest[device_id] = (mtime, path)
            return {dev: file.read_bytes() for dev, (_, file) in newest.items()}

        loaded = await self.hass.async_add_executor_job(_scan)
        for device_id, data in loaded.items():
            self.image_bytes[device_id] = data
            _LOGGER.info("Seeded %d bytes from archive for %s", len(data), device_id)

    async def _prune_old_media(self, max_age_days: int | None = None) -> None:
        """Delete archived media older than max_age_days. Best-effort."""
        media_path = self.media_path
        if media_path is None:
            return

        days: int = (
            max_age_days
            if max_age_days is not None
            else self.entry.options.get(CONF_MEDIA_RETENTION_DAYS, DEFAULT_MEDIA_RETENTION_DAYS)
        )

        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=days)).timestamp()

        def _prune() -> int:
            if not media_path.exists():
                return 0
            removed = 0
            for path in media_path.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
            # Sweep empty date dirs.
            for path in sorted(media_path.iterdir(), reverse=True):
                if path.is_dir() and not any(path.iterdir()):
                    with contextlib.suppress(OSError):
                        path.rmdir()
            return removed

        removed = await self.hass.async_add_executor_job(_prune)
        if removed:
            _LOGGER.info("Pruned %d archived files older than %d days", removed, days)

    async def archive_bytes(self, device_id: str, content: bytes, media_type: str) -> None:
        """Archive raw image bytes (e.g. a stream-extracted frame) to disk.

        Same naming scheme as _archive_media so the boot-time seed picks
        these up alongside snapshots and motion thumbnails.
        """
        media_path = self.media_path
        if media_path is None:
            return
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        date_dir = media_path / now.strftime("%Y-%m-%d")
        timestamp = int(now.timestamp())
        filepath = date_dir / f"{timestamp}_{device_id}_{media_type}.jpg"

        def _write() -> None:
            date_dir.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(content)

        try:
            await self.hass.async_add_executor_job(_write)
            _LOGGER.debug("Archived %d bytes to %s", len(content), filepath)
        except OSError:
            _LOGGER.debug("Failed to archive bytes for %s", device_id, exc_info=True)

    async def _cache_image_bytes(self, device_id: str, url: str) -> None:
        """Download an image URL and cache the bytes for the camera entity.

        Run as soon as the URL arrives via MQTT — Arlo's presigned URLs
        expire after a few hours and the camera tile has no other source
        of truth when the device is disarmed (no on-demand snapshots).
        """
        try:
            async with aiohttp.ClientSession() as session, session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Failed to cache image for %s: HTTP %s", device_id, resp.status)
                    return
                self.image_bytes[device_id] = await resp.read()
                _LOGGER.debug(
                    "Cached %d bytes for %s",
                    len(self.image_bytes[device_id]),
                    device_id,
                )
        except Exception:
            _LOGGER.debug("Image cache fetch failed for %s", device_id, exc_info=True)

    async def call_with_session_retry[T](
        self,
        op_name: str,
        op: Callable[[], Awaitable[T]],
    ) -> T:
        """Run an Arlo API call; on Invalid-Token (2015) relogin and retry once.

        Arlo can invalidate a session server-side at any time — usually
        because the user logged in elsewhere (the official app on a
        different phone, for example). The token sitting in our memory
        looks fine but the next request comes back with error 2015.

        We surface that as SessionExpiredError from the client, catch it
        here, force a silent relogin via the trust cookie, and retry the
        same call once. If the relogin itself fails (trust cookie also
        expired, rate limit, MFA required), let the auth exception
        bubble — the coordinator's update loop turns it into a HA
        reauth flow.
        """
        try:
            return await op()
        except SessionExpiredError:
            _LOGGER.warning(
                "Arlo rejected token during %s — re-logging in and retrying once",
                op_name,
            )
            await self._login_silent()
            return await op()

    def location_for_device(self, device: DeviceInfo) -> LocationState | None:
        """Resolve a device to its location, falling back when unmatched.

        When no location claims the device's gateway, fall back to the first
        location so single-location accounts (and accounts where Arlo omitted
        gatewayDeviceIds) still gate/track correctly. The fallback is loud only
        when there is real ambiguity (more than one location).
        """
        match = resolve_location_for_device(device, self.locations.values())
        if match is not None:
            return match
        if not self.locations:
            return None
        fallback = next(iter(self.locations.values()))
        gateway = device.parent_id or device.device_id
        if len(self.locations) > 1:
            _LOGGER.warning(
                "Device %s (gateway %s) matched no location's gatewayDeviceIds; "
                "falling back to location %s",
                device.device_id,
                gateway,
                fallback.location_id,
            )
        else:
            _LOGGER.debug(
                "Device %s (gateway %s) not in gatewayDeviceIds; using sole location %s",
                device.device_id,
                gateway,
                fallback.location_id,
            )
        return fallback

    def mode_for_device(self, device_id: str) -> str | None:
        """The active security mode of the device's location, or None."""
        device = next((d for d in self._devices if d.device_id == device_id), None)
        if device is None:
            return None
        location = self.location_for_device(device)
        return location.active_mode if location is not None else None

    def _apply_mode(self, location_id: str | None, mode: str) -> None:
        """Record a mode change from an MQTT event onto the right location.

        Events that carry a locationId route precisely. Events without one are
        only safe to apply when there is exactly one location; on multi-location
        accounts they are ambiguous and ignored rather than corrupting state.
        """
        if location_id is not None:
            location = self.locations.get(location_id)
            if location is not None:
                location.active_mode = mode
            else:
                _LOGGER.info("Mode event for unknown location %s: %s", location_id, mode)
            return
        if len(self.locations) == 1:
            next(iter(self.locations.values())).active_mode = mode
        elif len(self.locations) > 1:
            _LOGGER.debug(
                "Ambiguous mode push (no locationId) on multi-location account; ignoring"
            )

    def _log_subscribe_outcome(self) -> None:
        """Log the MQTT SUBACK summary in this (custom_components.eisenberg)
        namespace so HA's per-integration debug toggle surfaces grants,
        refusals and the user-topic verdict. The library-level eisenberg.mqtt
        grant logs sit in a namespace that toggle cannot reach.
        """
        if self._mqtt is None or self._mqtt.subscribe_outcome is None:
            return
        outcome = self._mqtt.subscribe_outcome
        _LOGGER.info(
            "MQTT SUBACK: %d granted, %d refused of %d",
            outcome.granted_count,
            outcome.refused_count,
            len(outcome.results),
        )
        if outcome.refused_topics:
            _LOGGER.warning("MQTT refused topic filters: %s", outcome.refused_topics)
        user_topic = f"u/{self.client.user_id}/in/#"
        result = outcome.result_for(user_topic)
        verdict = "ABSENT" if result is None else ("GRANTED" if result.granted else "REFUSED")
        _LOGGER.info("MQTT user topic %s = %s", user_topic, verdict)

    async def async_set_active_mode(self, location_id: str, mode: str) -> None:
        """Set the security mode of one location via the v3 location API.

        Pushes the new mode + that location's revision counter, then stores the
        revision the server returns. MQTT publishes the change shortly after,
        but we update the location optimistically so the UI reflects the
        request immediately.

        On any failure (typically a stale revision after another client changed
        the mode), refetch the live revision and retry once. Beyond that,
        surface the original error.
        """
        location = self.locations.get(location_id)
        if location is None:
            raise RuntimeError(f"Unknown location {location_id} — cannot set mode")

        from eisenberg import APIError

        async def _do_set() -> Any:
            return await self.client.set_active_mode(location_id, mode, location.mode_revision)

        try:
            result = await self.call_with_session_retry("set_active_mode", _do_set)
        except APIError:
            _LOGGER.info("set_active_mode failed, refreshing revision and retrying once")
            state = await self.client.get_active_mode(location_id)
            location.mode_revision = state.revision or 1
            result = await self.call_with_session_retry("set_active_mode", _do_set)

        if result.revision:
            location.mode_revision = result.revision
        if result.properties is not None:
            location.active_mode = result.properties.mode
        else:
            location.active_mode = mode
        self.async_set_updated_data(self.data or {})

    async def _save_cookies(self) -> None:
        """Persist trust cookies from the session to the config entry."""
        if self._http_session is None:
            return
        cookie_jar = self._http_session.cookie_jar
        cookies: list[dict[str, str]] = []
        for morsel in cookie_jar:
            if morsel.key.startswith("browser_trust_"):
                cookies.append(
                    {
                        "name": morsel.key,
                        "value": morsel.value,
                        "domain": morsel["domain"],
                        "path": morsel["path"],
                    }
                )
        if cookies:
            new_data = {**self.entry.data, CONF_TRUST_COOKIE: cookies}
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
            _LOGGER.info("Persisted %d trust cookies", len(cookies))

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
            await self._login_silent()
        except MfaRequired as err:
            raise ConfigEntryAuthFailed(
                "Trust cookie expired — re-authenticate to continue"
            ) from err
        except (AuthenticationError, RateLimitedError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        self._devices = await self.client.get_devices()

        # Collect every distinct xCloudId — accounts with cameras on
        # multiple base stations need a subscription per base or events
        # from the others never reach us. Order is preserved (dict trick)
        # so MQTT logs stay stable across restarts.
        x_cloud_ids = list(dict.fromkeys(d.x_cloud_id for d in self._devices))
        _LOGGER.info(
            "Discovered %d device(s) across %d base(s): xCloudIds=%s",
            len(self._devices),
            len(x_cloud_ids),
            x_cloud_ids,
        )
        for device in self._devices:
            _LOGGER.info(
                "  device id=%s name=%r model=%s cloud=%s mqttTopics=%d",
                device.device_id,
                device.device_name,
                device.model_id,
                device.x_cloud_id,
                len(device.allowed_mqtt_topics),
            )
            # Full topic list at DEBUG — the map of which resources each
            # model (camera vs doorbell) actually publishes on.
            _LOGGER.debug(
                "  device %s allowedMqttTopics=%s",
                device.device_id,
                device.allowed_mqtt_topics,
            )
        if len(x_cloud_ids) > 1:
            _LOGGER.warning(
                "Account spans %d Arlo base stations — subscribing to all. "
                "If any device entities stay unknown, verify their xCloudId is "
                "in the subscribed list above.",
                len(x_cloud_ids),
            )

        # Start MQTT
        if self.client.mqtt_url and self.client.user_id and self.client.token and x_cloud_ids:
            self._mqtt = MQTTEventStream(
                mqtt_url=self.client.mqtt_url,
                user_id=self.client.user_id,
                token=self.client.token,
                x_cloud_ids=x_cloud_ids,
                extra_topics=self._mqtt_extra_topics(),
                http_session=self._http_session,
            )
            self._register_mqtt_handlers()
            await self._mqtt.connect()
            self._log_subscribe_outcome()

        # Discover location + current mode + revision via v3 endpoints.
        # This runs before snapshot requests so we can skip them when the
        # camera is disarmed (Arlo refuses with error 4006).
        try:
            self.locations = {}
            for info in await self.client.get_locations():
                location = LocationState.from_info(info)
                try:
                    state = await self.client.get_active_mode(info.location_id)
                    location.mode_revision = state.revision or 1
                    if state.properties is not None:
                        location.active_mode = state.properties.mode
                except Exception:
                    _LOGGER.warning(
                        "Could not fetch mode for location %s",
                        info.location_id,
                        exc_info=True,
                    )
                self.locations[info.location_id] = location
            if len(self.locations) > 1:
                _LOGGER.info(
                    "Discovered %d locations: %s",
                    len(self.locations),
                    {
                        loc.location_id: {
                            "mode": loc.active_mode,
                            "gateways": loc.gateway_device_ids,
                        }
                        for loc in self.locations.values()
                    },
                )
            elif self.locations:
                sole = next(iter(self.locations.values()))
                _LOGGER.info(
                    "Initial mode: %s (revision=%s, location=%s)",
                    sole.active_mode,
                    sole.mode_revision,
                    sole.location_id,
                )
        except Exception:
            _LOGGER.warning("Could not fetch initial locations/mode", exc_info=True)

        # Request initial snapshots only when the camera's own location is
        # armed — Arlo refuses with error 4006 ("Invalid camera activity state
        # change") if the camera is in standby, so polling it just generates
        # noise. Gating per-device (not on a single global mode) is what makes
        # this correct on multi-location accounts.
        for device in self._devices:
            mode = self.mode_for_device(device.device_id)
            if mode and mode != "standby":
                try:
                    await self.client.request_snapshot(device.device_id)
                    _LOGGER.debug("Requested initial snapshot for %s", device.device_id)
                except Exception:
                    _LOGGER.debug("Could not request snapshot for %s", device.device_id)

        # Restore the dashboard tile from the archive and clean up old
        # files. Both are no-ops when media archival is disabled.
        await self._seed_image_cache_from_disk()
        await self._prune_old_media()

    def _register_mqtt_handlers(self) -> None:
        """Register MQTT topic handlers."""
        if self._mqtt is None:
            return

        # Camera state updates
        self._mqtt.on("d/+/out/cameras/+/is", self._handle_camera_state)

        # Full device-properties dump — carries spotlight, battery, WiFi,
        # motion zones etc. Topic name is misleading; same handler works
        # because `properties` is a superset of the partial-update shape.
        self._mqtt.on("d/+/out/cameras/+/privacyZones/is", self._handle_camera_state)

        # Doorbell state updates. Video doorbells (e.g. FB1001A) are a
        # distinct `doorbells` resource — not `cameras` — and publish their
        # motionDetected/battery/signal on this topic. Same payload shape as
        # cameras, so the same handler parses it; topic part[4] is still the
        # deviceId. Without this, doorbell entities never update (issue #10).
        self._mqtt.on("d/+/out/doorbells/+/is", self._handle_camera_state)
        self._mqtt.on("d/+/out/doorbells/+/privacyZones/is", self._handle_camera_state)

        # Snapshot available — cameras and doorbells both emit this.
        self._mqtt.on(
            "d/+/out/cameras/+/fullFrameSnapshotAvailable",
            self._handle_snapshot,
        )
        self._mqtt.on(
            "d/+/out/doorbells/+/fullFrameSnapshotAvailable",
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

        # Base station heartbeat (frequent, includes connectionState)
        self._mqtt.on("d/+/out/basestation/is", self._handle_basestation)

        # Per-device states broadcast — activeMode mirrors the automation
        # topic in friendly name form, plus motionStart actions config.
        self._mqtt.on("d/+/out/devices/+/states/is", self._handle_device_states)

        # Geofence configuration push — informational only, just absorb
        # the topic so it doesn't show up as Unhandled.
        self._mqtt.on("u/+/in/automation/geofences/is", self._handle_geofences)

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
            raw_spot: Any = (
                cast("dict[str, Any]", properties).get("spotlight")
                if isinstance(properties, dict)
                else None
            )
            if isinstance(raw_spot, dict):
                spotlight_raw = cast("dict[str, Any]", raw_spot)
                try:
                    self.spotlight_states[device_id] = SpotlightState.model_validate(spotlight_raw)
                    _LOGGER.debug(
                        "Spotlight %s: enabled=%s intensity=%s",
                        device_id,
                        self.spotlight_states[device_id].enabled,
                        self.spotlight_states[device_id].intensity,
                    )
                except Exception:
                    _LOGGER.warning(
                        "Failed to parse spotlight for %s: %s",
                        device_id,
                        json.dumps(spotlight_raw)[:200],
                    )
            # Surface Arlo's "Invalid camera activity state change" error
            # once at INFO so it's clear why disarmed snapshots are silent.
            err = payload.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message"))  # pyright: ignore[reportUnknownArgumentType,reportUnknownMemberType]
                _LOGGER.info("Camera %s rejected request: %s", device_id, msg)
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

            # Cache bytes immediately — presigned URLs expire.
            await self._cache_image_bytes(device_id, snap.presigned_url)
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
                    await self._cache_image_bytes(event.device_id, event.thumbnail_url)
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
                self._apply_mode(event.location_id, event.active_mode)
                _LOGGER.debug(
                    "Mode change: location=%s mode=%s", event.location_id, event.active_mode
                )
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
            location_id = payload.get("locationId")
            self._apply_mode(
                location_id if isinstance(location_id, str) else None,
                mode.properties.mode,
            )
            _LOGGER.debug("Active mode update: %s", mode.properties.mode)
            self.async_set_updated_data(self.data or {})
        except Exception:
            _LOGGER.warning(
                "Failed to parse active mode: %s",
                json.dumps(payload)[:500],
            )

    async def _handle_connectivity(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle connectivity updates."""
        _LOGGER.debug("Connectivity update: %s", json.dumps(payload)[:200])

    async def _handle_device_states(self, topic: str, payload: dict[str, Any]) -> None:
        """Per-device state broadcast — currently we only care about activeMode.

        Topic: d/{xCloudId}/out/devices/{deviceId}/states/is. The states
        block carries the friendly activeMode (armAway/armHome/standby); we
        mirror it onto the broadcasting device's location so that location's
        select stays in sync if a different client changes it.
        """
        states = payload.get("states")
        if not isinstance(states, dict):
            return
        try:
            active_mode = states["activeMode"]  # pyright: ignore[reportUnknownVariableType]
        except (KeyError, TypeError):
            return
        if isinstance(active_mode, str):
            # Topic: d/{xCloudId}/out/devices/{deviceId}/states/is — resolve the
            # broadcasting device to its location so multi-location accounts
            # update the right one. Falls back to the sole location otherwise.
            parts = topic.split("/")
            device_id = parts[4] if len(parts) > 4 else None
            device = (
                next((d for d in self._devices if d.device_id == device_id), None)
                if device_id is not None
                else None
            )
            location = self.location_for_device(device) if device is not None else None
            self._apply_mode(location.location_id if location is not None else None, active_mode)
            self.async_set_updated_data(self.data or {})

    async def _handle_geofences(self, topic: str, payload: dict[str, Any]) -> None:
        """Geofence config push from the Arlo app — informational only."""
        _LOGGER.debug("Geofence update on %s", topic)

    async def _handle_basestation(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle base station heartbeat / state.

        Topic shape: d/{xCloudId}/out/basestation/is. The `from` field
        identifies the gateway. Empty payloads are ack frames; ones with
        `properties.connectionState` carry the link state we surface as a
        binary sensor.
        """
        gateway_id = payload.get("from")
        if not isinstance(gateway_id, str):
            return
        properties: Any = payload.get("properties") or {}
        try:
            state = BasestationState.model_validate(properties)
        except Exception:
            _LOGGER.warning(
                "Failed to parse basestation state: %s",
                json.dumps(payload)[:300],
            )
            return
        if state.connection_state is None:
            # ack-only frame; nothing to report
            return
        prev = self.basestation_connection.get(gateway_id)
        self.basestation_connection[gateway_id] = state.connection_state
        if prev != state.connection_state:
            _LOGGER.info(
                "Base station %s connectionState: %s",
                gateway_id,
                state.connection_state,
            )
        self.async_set_updated_data(self.data or {})

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
        filename = f"{timestamp}_{device_id}_{media_type}.{ext}"
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
                await self._login_silent()
            except MfaRequired as err:
                raise ConfigEntryAuthFailed(
                    "Trust cookie expired — re-authenticate to continue"
                ) from err
            except (AuthenticationError, RateLimitedError) as err:
                raise ConfigEntryAuthFailed(str(err)) from err

        # Prune old archived media on every health tick (cheap when empty).
        await self._prune_old_media()

        # MQTT reconnect
        if self._mqtt is None and self.client.mqtt_url:
            _LOGGER.info("Reconnecting MQTT")
            try:
                x_cloud_ids = list(dict.fromkeys(d.x_cloud_id for d in self._devices))
                if (
                    self.client.user_id
                    and self.client.token
                    and self._http_session
                    and x_cloud_ids
                ):
                    self._mqtt = MQTTEventStream(
                        mqtt_url=self.client.mqtt_url,
                        user_id=self.client.user_id,
                        token=self.client.token,
                        x_cloud_ids=x_cloud_ids,
                        extra_topics=self._mqtt_extra_topics(),
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
