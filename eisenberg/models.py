"""Pydantic models for all Arlo API and MQTT payloads.

These are the parsing boundary. JSON from the Arlo API/MQTT gets
validated here. If parsing fails, it blows up here with a
ValidationError. Inside the codebase, types guarantee correctness.

Field aliases map camelCase JSON keys to snake_case Python attributes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ArloMode(StrEnum):
    """Arlo security modes."""

    ARM_AWAY = "armAway"
    ARM_HOME = "armHome"
    STANDBY = "standby"


class FactorType(StrEnum):
    """MFA second-factor types Arlo exposes via PingOne SDK."""

    PUSH = "PUSH"
    EMAIL = "EMAIL"
    SMS = "SMS"


class SecondFactor(BaseModel):
    """One MFA factor as returned by /api/getFactors or /api/startAuth.

    factor_role is "PRIMARY" for the user's preferred factor and
    "SECONDARY" for fallbacks. display_name is what the picker UI
    renders to the user (a device nickname for PUSH, a masked address
    like "ma****@example.com" for EMAIL).
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    factor_id: str = Field(alias="factorId")
    factor_type: FactorType = Field(alias="factorType")
    display_name: str = Field(alias="displayName")
    factor_nickname: str | None = Field(None, alias="factorNickname")
    factor_role: str = Field(alias="factorRole")


class DeviceState(BaseModel):
    """Camera device state update from MQTT.

    These arrive as partial updates — only changed fields are present.
    All fields are optional because any subset can appear.
    """

    model_config = {"populate_by_name": True}

    motion_detected: bool | None = Field(None, alias="motionDetected")
    activity_state: str | None = Field(None, alias="activityState")
    date_started: int | None = Field(None, alias="dateStarted")
    signal_strength: int | None = Field(None, alias="signalStrength")
    battery_level: int | None = Field(None, alias="batteryLevel")
    charging_state: str | None = Field(None, alias="chargingState")


class MediaMeta(BaseModel):
    """Video metadata from a motion recording."""

    model_config = {"populate_by_name": True}

    codec_tag_string: str | None = Field(None, alias="codec_tag_string")
    height: str | None = None
    width: str | None = None
    bit_rate: str | None = Field(None, alias="bit_rate")


class MotionEvent(BaseModel):
    """Motion detection event with AI classification and media URLs.

    From u/{userId}/in/feed/live with type="motion".
    """

    model_config = {"populate_by_name": True}

    type: str
    device_id: str = Field(alias="deviceId")
    obj_category: str | None = Field(None, alias="objCategory")
    obj_categories: list[str] | None = Field(None, alias="objCategories")
    obj_region: str | None = Field(None, alias="objRegion")
    duration: str | None = None
    content_url: str | None = Field(None, alias="contentUrl")
    thumbnail_url: str | None = Field(None, alias="thumbnailUrl")
    content_type: str | None = Field(None, alias="contentType")
    media_meta: MediaMeta | None = Field(None, alias="mediaMeta")
    active_mode: ArloMode | None = Field(None, alias="activeMode")
    utc_created_date: int = Field(alias="utcCreatedDate")
    date: str | None = None
    time_zone: str | None = Field(None, alias="timeZone")
    resource: str | None = None
    owner_id: str | None = Field(None, alias="ownerId")
    user_id: str | None = Field(None, alias="userId")
    feed_object_count: int | None = Field(None, alias="feedObjectCount")
    media_object_count: int | None = Field(None, alias="mediaObjectCount")
    location_id: str | None = Field(None, alias="locationId")
    feed_id: str | None = Field(None, alias="feedId")
    unique_id: str | None = Field(None, alias="uniqueId")
    model_id: str | None = Field(None, alias="modelId")
    state: str | None = None
    action: str | None = None
    donated: bool | None = None


class ModeChangeEvent(BaseModel):
    """Mode change feed event.

    From u/{userId}/in/feed/live with type="modeChange".
    """

    model_config = {"populate_by_name": True}

    type: str
    active_mode: ArloMode = Field(alias="activeMode")
    device_id: str = Field(alias="deviceId")
    utc_created_date: int = Field(alias="utcCreatedDate")
    date: str | None = None
    time_zone: str | None = Field(None, alias="timeZone")
    resource: str | None = None
    owner_id: str | None = Field(None, alias="ownerId")
    user_id: str | None = Field(None, alias="userId")
    feed_object_count: int | None = Field(None, alias="feedObjectCount")
    media_object_count: int | None = Field(None, alias="mediaObjectCount")
    location_id: str | None = Field(None, alias="locationId")
    feed_id: str | None = Field(None, alias="feedId")
    unique_id: str | None = Field(None, alias="uniqueId")
    state: str | None = None
    action: str | None = None
    donated: bool | None = None


class LocationInfo(BaseModel):
    """One entry from GET /hmsdevicemanagement/users/{userId}/locations."""

    model_config = {"populate_by_name": True, "extra": "ignore"}

    location_id: str = Field(alias="locationId")
    location_name: str | None = Field(None, alias="locationName")


class ActiveModeStateProperties(BaseModel):
    """Inner properties of the v3 activeMode REST response."""

    model_config = {"populate_by_name": True, "extra": "ignore"}

    mode: str


class ActiveModeState(BaseModel):
    """Response from GET/PUT /hmsweb/automation/v3/activeMode.

    Carries the current mode and the revision number we must echo back on
    the next PUT so concurrent updates don't clobber each other.
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    properties: ActiveModeStateProperties | None = None
    revision: int = 0


class ActiveModeProperties(BaseModel):
    """Inner properties of an active mode update."""

    mode: ArloMode


class ActiveMode(BaseModel):
    """Active mode state from MQTT automation topic."""

    properties: ActiveModeProperties
    revision: int


class SirenState(BaseModel):
    """Siren state from MQTT."""

    model_config = {"populate_by_name": True}

    siren_state: str = Field(alias="sirenState")
    siren_trigger: str | None = Field(None, alias="sirenTrigger")
    duration: int | None = None
    volume: int | None = None
    pattern: str | None = None
    siren_timestamp: int | None = Field(None, alias="sirenTimestamp")

    @property
    def is_on(self) -> bool:
        return self.siren_state == "on"


class SpotlightState(BaseModel):
    """Camera-integrated spotlight state.

    Appears under the `spotlight` key in camera property payloads
    (both the `cameras/{id}/is` partial updates and the full
    `cameras/{id}/privacyZones/is` device dump). intensity is on a
    0-100 scale.
    """

    model_config = {"populate_by_name": True}

    enabled: bool
    intensity: int | None = None


class SnapshotAvailable(BaseModel):
    """Snapshot URL notification from MQTT."""

    model_config = {"populate_by_name": True}

    presigned_url: str = Field(alias="presignedFullFrameSnapshotUrl")
    disable_privacy_zones: bool | None = Field(None, alias="disablePrivacyZones")


class MediaUpload(BaseModel):
    """Media upload notification from MQTT."""

    model_config = {"populate_by_name": True}

    resource: str
    device_id: str = Field(alias="deviceId")
    created_date: str | None = Field(None, alias="createdDate")
    owner_id: str | None = Field(None, alias="ownerId")
    media_object_count: int | None = Field(None, alias="mediaObjectCount")
    presigned_content_url: str | None = Field(None, alias="presignedContentUrl")
    presigned_thumbnail_url: str | None = Field(None, alias="presignedThumbnailUrl")
    presigned_last_image_url: str | None = Field(None, alias="presignedLastImageUrl")
    unique_id: str | None = Field(None, alias="uniqueId")
    recording_stopped: bool | None = Field(None, alias="recordingStopped")


class WifiInfo(BaseModel):
    """WiFi connection details."""

    model_config = {"populate_by_name": True}

    type: str | None = None
    connected: bool
    ssid: str | None = None
    wifi_rssi: int | None = Field(None, alias="wifiRssi")
    signal_strength: int | None = Field(None, alias="signalStrength")
    ip_addr: str | None = Field(None, alias="ipAddr")
    connection_state: str | None = Field(None, alias="connectionState")


class Connectivity(BaseModel):
    """Connectivity state from MQTT."""

    connectivity: list[WifiInfo]


class BasestationState(BaseModel):
    """Properties from MQTT topic d/{xCloudId}/out/basestation/is.

    The base periodically heartbeats with `connectionState: "available"`
    when reachable. Empty payloads (`properties: {}`) are also valid and
    indicate ack-only frames.
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    connection_state: str | None = Field(None, alias="connectionState")


class StreamResponse(BaseModel):
    """Response from POST /hmsweb/users/devices/startStream."""

    model_config = {"populate_by_name": True}

    url: str
    sip_call_info: dict[str, Any] | None = Field(None, alias="sipCallInfo")
    ice_servers: dict[str, Any] | None = Field(None, alias="iceServers")


class DeviceInfo(BaseModel):
    """Device info from the devices list API."""

    model_config = {"populate_by_name": True}

    device_id: str = Field(alias="deviceId")
    device_name: str = Field(alias="deviceName")
    model_id: str = Field(alias="modelId")
    x_cloud_id: str = Field(alias="xCloudId")
    user_id: str | None = Field(None, alias="userId")
    # Exact MQTT topic filters this device declares it will publish on.
    # Authoritative across device types (cameras, doorbells, base-less
    # cameras) and ACL-safe — the broker grants these even when a broad
    # `d/{xCloudId}/out/#` wildcard would be refused. Empty when absent.
    allowed_mqtt_topics: list[str] = Field(default_factory=list, alias="allowedMqttTopics")
    properties: dict[str, Any] | None = None
    connectivity: dict[str, Any] | None = None
