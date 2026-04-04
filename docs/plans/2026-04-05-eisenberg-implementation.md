# Eisenberg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Home Assistant custom component that connects to Arlo cameras via their cloud API (REST + MQTT), providing real-time motion/AI detection, live RTSP streaming, snapshots, siren control, and media archival.

**Architecture:** Two packages in one repo. `eisenberg/` is a standalone async API client (Pydantic models, aiohttp, raw MQTT 3.1.1 packets). `custom_components/eisenberg/` is the HA integration that consumes it — event-driven via MQTT (not polling), with camera, binary sensor, sensor, and switch entities. Media archival downloads motion clips to HA media storage.

**Tech Stack:** Python 3.12+, aiohttp, Pydantic 2.x, pytest + pytest-asyncio, pyright strict, ruff. No external MQTT library.

**Spec:** `docs/specs/2026-04-05-eisenberg-design.md`

**Reference implementation:** `../ha-arlo/login.py` (working auth prototype in sibling directory)

---

## Task 1: Exceptions

**Files:**
- Create: `eisenberg/exceptions.py`
- Test: `tests/test_exceptions.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_exceptions.py
"""Tests for eisenberg exceptions hierarchy."""

from eisenberg.exceptions import (
    EisenbergError,
    AuthenticationError,
    PushApprovalRequired,
    SessionExpiredError,
    APIError,
    MQTTConnectionError,
)


def test_base_exception_is_exception() -> None:
    assert issubclass(EisenbergError, Exception)


def test_authentication_error_has_message() -> None:
    err = AuthenticationError("bad creds")
    assert str(err) == "bad creds"
    assert isinstance(err, EisenbergError)


def test_push_approval_required_carries_factors() -> None:
    factors = [{"factorType": "PUSH", "displayName": "Phone"}]
    err = PushApprovalRequired(
        factor_auth_code="abc123",
        factors=factors,
    )
    assert err.factor_auth_code == "abc123"
    assert err.factors == factors
    assert isinstance(err, AuthenticationError)


def test_session_expired_is_auth_error() -> None:
    assert issubclass(SessionExpiredError, AuthenticationError)


def test_api_error_has_code_and_message() -> None:
    err = APIError(code="2001", message="Invalid content")
    assert err.code == "2001"
    assert err.message == "Invalid content"
    assert "2001" in str(err)
    assert isinstance(err, EisenbergError)


def test_mqtt_connection_error() -> None:
    assert issubclass(MQTTConnectionError, EisenbergError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_exceptions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eisenberg.exceptions'`

- [ ] **Step 3: Write implementation**

```python
# eisenberg/exceptions.py
"""Eisenberg exception hierarchy.

All exceptions derive from EisenbergError. Auth errors form a sub-tree
so callers can catch broadly or narrowly.
"""

from __future__ import annotations

from typing import Any


class EisenbergError(Exception):
    """Base exception for all eisenberg errors."""


class AuthenticationError(EisenbergError):
    """Authentication failed (wrong credentials, expired token, etc.)."""


class PushApprovalRequired(AuthenticationError):
    """2FA push approval needed. Carries factor info for the UI to display."""

    def __init__(
        self,
        factor_auth_code: str,
        factors: list[dict[str, Any]],
    ) -> None:
        self.factor_auth_code = factor_auth_code
        self.factors = factors
        super().__init__("Push approval required")


class SessionExpiredError(AuthenticationError):
    """Token expired, re-auth needed."""


class APIError(EisenbergError):
    """Arlo API returned an error response."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Arlo API error {code}: {message}")


class MQTTConnectionError(EisenbergError):
    """Failed to connect or maintain MQTT WebSocket connection."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_exceptions.py -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Update exports**

```python
# eisenberg/__init__.py
"""Eisenberg -- async Python client for the Arlo camera API."""

from .exceptions import (
    APIError,
    AuthenticationError,
    EisenbergError,
    MQTTConnectionError,
    PushApprovalRequired,
    SessionExpiredError,
)

__all__ = [
    "APIError",
    "AuthenticationError",
    "EisenbergError",
    "MQTTConnectionError",
    "PushApprovalRequired",
    "SessionExpiredError",
]
```

- [ ] **Step 6: Commit**

```bash
git add eisenberg/exceptions.py eisenberg/__init__.py tests/test_exceptions.py
git commit -m "feat: add exception hierarchy"
```

---

## Task 2: Pydantic Models

**Files:**
- Create: `eisenberg/models.py`
- Test: `tests/test_models.py`

All models parse the exact JSON payloads captured from the live MQTT stream. Tests use real payload shapes from the spec.

- [ ] **Step 1: Write tests for all models**

```python
# tests/test_models.py
"""Tests for eisenberg Pydantic models.

All test payloads are based on real MQTT messages captured from a live
Arlo system. See docs/specs/2026-04-05-eisenberg-design.md for context.
"""

from eisenberg.models import (
    ActiveMode,
    ArloMode,
    Connectivity,
    DeviceInfo,
    DeviceState,
    MediaMeta,
    MediaUpload,
    ModeChangeEvent,
    MotionEvent,
    SirenState,
    SnapshotAvailable,
    StreamResponse,
    WifiInfo,
)


class TestDeviceState:
    def test_motion_detected(self) -> None:
        state = DeviceState.model_validate({
            "motionDetected": True,
        })
        assert state.motion_detected is True
        assert state.activity_state is None

    def test_activity_state(self) -> None:
        state = DeviceState.model_validate({
            "activityState": "alertStreamActive",
            "dateStarted": 1775341447000,
        })
        assert state.activity_state == "alertStreamActive"
        assert state.date_started == 1775341447000

    def test_signal_strength(self) -> None:
        state = DeviceState.model_validate({
            "signalStrength": 2,
        })
        assert state.signal_strength == 2

    def test_battery_level(self) -> None:
        state = DeviceState.model_validate({
            "batteryLevel": 17,
            "chargingState": "Off",
        })
        assert state.battery_level == 17
        assert state.charging_state == "Off"


class TestMotionEvent:
    PAYLOAD = {
        "donated": False,
        "date": "20260405",
        "resource": "feedNotification",
        "type": "motion",
        "objCategory": "Person",
        "objCategories": ["Person"],
        "objRegion": "0.598,0.269,0.746,0.770",
        "deviceId": "AGSEXAMPLE001",
        "duration": "00:00:26",
        "contentUrl": "https://example.com/video.mp4",
        "thumbnailUrl": "https://example.com/thumb.jpg",
        "contentType": "video/mp4",
        "mediaMeta": {
            "codec_tag_string": "hvc1",
            "height": "1080",
            "width": "1920",
            "bit_rate": "790192",
        },
        "activeMode": "armAway",
        "utcCreatedDate": 1775341447317,
        "timeZone": "Europe/Rome",
        "ownerId": "USER123",
        "userId": "USER123",
        "feedObjectCount": 1,
        "mediaObjectCount": 1,
        "locationId": "loc-uuid",
        "feedId": "feed-id",
        "uniqueId": "unique-id",
        "modelId": "VMC2052A",
        "state": "new",
        "action": "new",
    }

    def test_parse_full_motion_event(self) -> None:
        event = MotionEvent.model_validate(self.PAYLOAD)
        assert event.type == "motion"
        assert event.obj_category == "Person"
        assert event.obj_categories == ["Person"]
        assert event.obj_region == "0.598,0.269,0.746,0.770"
        assert event.device_id == "AGSEXAMPLE001"
        assert event.duration == "00:00:26"
        assert event.content_url == "https://example.com/video.mp4"
        assert event.thumbnail_url == "https://example.com/thumb.jpg"
        assert event.utc_created_date == 1775341447317
        assert event.active_mode == ArloMode.ARM_AWAY

    def test_media_meta(self) -> None:
        event = MotionEvent.model_validate(self.PAYLOAD)
        assert event.media_meta is not None
        assert event.media_meta.width == "1920"
        assert event.media_meta.height == "1080"


class TestModeChangeEvent:
    def test_parse_mode_change(self) -> None:
        event = ModeChangeEvent.model_validate({
            "donated": False,
            "date": "20260404",
            "resource": "feedNotification",
            "type": "modeChange",
            "activeMode": "standby",
            "deviceId": "loc-uuid",
            "utcCreatedDate": 1775339557383,
            "timeZone": "Europe/Rome",
            "ownerId": "USER123",
            "userId": "USER123",
            "feedObjectCount": 0,
            "mediaObjectCount": 0,
            "locationId": "loc-uuid",
            "feedId": "feed-id",
            "uniqueId": "unique-id",
            "state": "new",
            "action": "new",
        })
        assert event.type == "modeChange"
        assert event.active_mode == ArloMode.STANDBY


class TestActiveMode:
    def test_parse(self) -> None:
        mode = ActiveMode.model_validate({
            "properties": {"mode": "armAway"},
            "revision": 1775339550697,
        })
        assert mode.properties.mode == ArloMode.ARM_AWAY
        assert mode.revision == 1775339550697


class TestSirenState:
    def test_siren_on(self) -> None:
        state = SirenState.model_validate({
            "sirenState": "on",
            "sirenTrigger": "manual",
            "duration": 180,
            "volume": 8,
            "pattern": "alarm",
            "sirenTimestamp": 1775340116000,
        })
        assert state.siren_state == "on"
        assert state.is_on is True
        assert state.duration == 180

    def test_siren_off(self) -> None:
        state = SirenState.model_validate({
            "sirenState": "off",
            "duration": 0,
            "sirenTimestamp": 1775340118000,
        })
        assert state.is_on is False


class TestSnapshotAvailable:
    def test_parse(self) -> None:
        snap = SnapshotAvailable.model_validate({
            "presignedFullFrameSnapshotUrl": "https://example.com/snap.jpg",
            "disablePrivacyZones": False,
        })
        assert snap.presigned_url == "https://example.com/snap.jpg"


class TestMediaUpload:
    def test_parse(self) -> None:
        upload = MediaUpload.model_validate({
            "resource": "mediaUploadNotification",
            "deviceId": "AGSEXAMPLE001",
            "createdDate": "20260405",
            "ownerId": "USER123",
            "mediaObjectCount": 1,
            "presignedContentUrl": "https://example.com/video.mp4",
            "presignedThumbnailUrl": "https://example.com/thumb.jpg",
            "presignedLastImageUrl": "https://example.com/last.jpg",
            "uniqueId": "USER123_AGSEXAMPLE001",
            "recordingStopped": True,
        })
        assert upload.device_id == "AGSEXAMPLE001"
        assert upload.presigned_content_url == "https://example.com/video.mp4"
        assert upload.recording_stopped is True


class TestConnectivity:
    def test_parse(self) -> None:
        conn = Connectivity.model_validate({
            "connectivity": [{
                "type": "wifi",
                "connected": True,
                "ssid": "TestNet",
                "wifiRssi": -67,
                "signalStrength": 2,
                "ipAddr": "192.168.1.100",
                "connectionState": "Connected",
            }],
        })
        assert len(conn.connectivity) == 1
        wifi = conn.connectivity[0]
        assert wifi.ssid == "TestNet"
        assert wifi.wifi_rssi == -67
        assert wifi.signal_strength == 2


class TestStreamResponse:
    def test_parse(self) -> None:
        resp = StreamResponse.model_validate({
            "url": "rtsp://wowza.arlo.com:443/vzmodulelive/CAM123",
            "sipCallInfo": {"id": "call123"},
            "iceServers": {"data": []},
        })
        assert resp.url == "rtsp://wowza.arlo.com:443/vzmodulelive/CAM123"


class TestDeviceInfo:
    def test_parse(self) -> None:
        info = DeviceInfo.model_validate({
            "deviceId": "AGSEXAMPLE001",
            "deviceName": "Patio",
            "modelId": "VMC2052A",
            "xCloudId": "XCLOUD-0000-000-000000000",
            "userId": "USER123",
            "properties": {
                "batteryLevel": 17,
                "state": "idle",
            },
            "connectivity": {"connected": True},
        })
        assert info.device_id == "AGSEXAMPLE001"
        assert info.device_name == "Patio"
        assert info.model_id == "VMC2052A"
        assert info.x_cloud_id == "XCLOUD-0000-000-000000000"


class TestArloMode:
    def test_known_modes(self) -> None:
        assert ArloMode.ARM_AWAY == "armAway"
        assert ArloMode.ARM_HOME == "armHome"
        assert ArloMode.STANDBY == "standby"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write all models**

```python
# eisenberg/models.py
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


# ---------------------------------------------------------------------------
# Device state (from d/.../cameras/{id}/is)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Feed events (from u/.../feed/live)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Active mode (from u/.../automation/activeMode/is)
# ---------------------------------------------------------------------------


class ActiveModeProperties(BaseModel):
    """Inner properties of an active mode update."""

    mode: ArloMode


class ActiveMode(BaseModel):
    """Active mode state from MQTT automation topic."""

    properties: ActiveModeProperties
    revision: int


# ---------------------------------------------------------------------------
# Siren (from d/.../siren/{id}/is)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Snapshot (from d/.../cameras/{id}/fullFrameSnapshotAvailable)
# ---------------------------------------------------------------------------


class SnapshotAvailable(BaseModel):
    """Snapshot URL notification from MQTT."""

    model_config = {"populate_by_name": True}

    presigned_url: str = Field(alias="presignedFullFrameSnapshotUrl")
    disable_privacy_zones: bool | None = Field(
        None, alias="disablePrivacyZones"
    )


# ---------------------------------------------------------------------------
# Media upload (from u/.../library/add)
# ---------------------------------------------------------------------------


class MediaUpload(BaseModel):
    """Media upload notification from MQTT."""

    model_config = {"populate_by_name": True}

    resource: str
    device_id: str = Field(alias="deviceId")
    created_date: str | None = Field(None, alias="createdDate")
    owner_id: str | None = Field(None, alias="ownerId")
    media_object_count: int | None = Field(None, alias="mediaObjectCount")
    presigned_content_url: str | None = Field(
        None, alias="presignedContentUrl"
    )
    presigned_thumbnail_url: str | None = Field(
        None, alias="presignedThumbnailUrl"
    )
    presigned_last_image_url: str | None = Field(
        None, alias="presignedLastImageUrl"
    )
    unique_id: str | None = Field(None, alias="uniqueId")
    recording_stopped: bool | None = Field(None, alias="recordingStopped")


# ---------------------------------------------------------------------------
# Connectivity (from d/.../basestation/connectivity/is)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Stream response (from startStream REST call)
# ---------------------------------------------------------------------------


class StreamResponse(BaseModel):
    """Response from POST /hmsweb/users/devices/startStream."""

    model_config = {"populate_by_name": True}

    url: str
    sip_call_info: dict[str, Any] | None = Field(None, alias="sipCallInfo")
    ice_servers: dict[str, Any] | None = Field(None, alias="iceServers")


# ---------------------------------------------------------------------------
# Device info (from GET /hmsweb/v2/users/devices)
# ---------------------------------------------------------------------------


class DeviceInfo(BaseModel):
    """Device info from the devices list API."""

    model_config = {"populate_by_name": True}

    device_id: str = Field(alias="deviceId")
    device_name: str = Field(alias="deviceName")
    model_id: str = Field(alias="modelId")
    x_cloud_id: str = Field(alias="xCloudId")
    user_id: str | None = Field(None, alias="userId")
    properties: dict[str, Any] | None = None
    connectivity: dict[str, Any] | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_models.py -v`
Expected: all tests PASS

- [ ] **Step 5: Update exports in `__init__.py`**

Add model exports to `eisenberg/__init__.py`:

```python
# eisenberg/__init__.py
"""Eisenberg -- async Python client for the Arlo camera API."""

from .exceptions import (
    APIError,
    AuthenticationError,
    EisenbergError,
    MQTTConnectionError,
    PushApprovalRequired,
    SessionExpiredError,
)
from .models import (
    ActiveMode,
    ArloMode,
    Connectivity,
    DeviceInfo,
    DeviceState,
    MediaUpload,
    ModeChangeEvent,
    MotionEvent,
    SirenState,
    SnapshotAvailable,
    StreamResponse,
)

__all__ = [
    "APIError",
    "ActiveMode",
    "ArloMode",
    "AuthenticationError",
    "Connectivity",
    "DeviceInfo",
    "DeviceState",
    "EisenbergError",
    "MQTTConnectionError",
    "MediaUpload",
    "ModeChangeEvent",
    "MotionEvent",
    "PushApprovalRequired",
    "SessionExpiredError",
    "SirenState",
    "SnapshotAvailable",
    "StreamResponse",
]
```

- [ ] **Step 6: Commit**

```bash
git add eisenberg/models.py eisenberg/__init__.py tests/test_models.py
git commit -m "feat: add Pydantic models for all Arlo API/MQTT payloads"
```

---

## Task 3: MQTT Packet Codec

**Files:**
- Create: `eisenberg/mqtt_codec.py`
- Test: `tests/test_mqtt_codec.py`

Raw MQTT 3.1.1 binary packet construction and parsing. No external MQTT library — the protocol is simple enough for the subset we need (CONNECT, CONNACK, SUBSCRIBE, SUBACK, PUBLISH, PINGREQ, PINGRESP, DISCONNECT).

- [ ] **Step 1: Write tests**

```python
# tests/test_mqtt_codec.py
"""Tests for raw MQTT 3.1.1 packet codec."""

from eisenberg.mqtt_codec import (
    MQTTPublish,
    build_connect,
    build_disconnect,
    build_pingreq,
    build_subscribe,
    parse_connack,
    parse_packet_type,
    parse_publish,
)


class TestBuildConnect:
    def test_builds_valid_packet(self) -> None:
        pkt = build_connect(
            client_id="test-client",
            username="user123",
            password="token-abc",
            keepalive=60,
        )
        # First byte: CONNECT packet type (1 << 4 = 0x10)
        assert pkt[0] == 0x10
        # Contains protocol name "MQTT"
        assert b"MQTT" in pkt
        # Contains client_id, username, password
        assert b"test-client" in pkt
        assert b"user123" in pkt
        assert b"token-abc" in pkt

    def test_keepalive_encoded(self) -> None:
        pkt = build_connect(
            client_id="c",
            username="u",
            password="p",
            keepalive=60,
        )
        # Keepalive is 2 bytes big-endian (0x00, 0x3c = 60)
        assert b"\x00\x3c" in pkt


class TestBuildSubscribe:
    def test_builds_valid_packet(self) -> None:
        pkt = build_subscribe(
            packet_id=1,
            topics=["d/cloud123/out/#", "u/user123/in/#"],
        )
        # First byte: SUBSCRIBE type (8 << 4 | 0x02 = 0x82)
        assert pkt[0] == 0x82
        assert b"d/cloud123/out/#" in pkt
        assert b"u/user123/in/#" in pkt


class TestBuildPingreq:
    def test_fixed_packet(self) -> None:
        assert build_pingreq() == bytes([0xC0, 0x00])


class TestBuildDisconnect:
    def test_fixed_packet(self) -> None:
        assert build_disconnect() == bytes([0xE0, 0x00])


class TestParsePacketType:
    def test_connack(self) -> None:
        assert parse_packet_type(bytes([0x20, 0x02, 0x00, 0x00])) == 2

    def test_suback(self) -> None:
        assert parse_packet_type(bytes([0x90, 0x04, 0x00, 0x01, 0x00, 0x00])) == 9

    def test_publish(self) -> None:
        assert parse_packet_type(bytes([0x30, 0x05, 0x00, 0x01, 0x74, 0x7B, 0x7D])) == 3

    def test_pingresp(self) -> None:
        assert parse_packet_type(bytes([0xD0, 0x00])) == 13


class TestParseConnack:
    def test_success(self) -> None:
        rc = parse_connack(bytes([0x20, 0x02, 0x00, 0x00]))
        assert rc == 0

    def test_bad_credentials(self) -> None:
        rc = parse_connack(bytes([0x20, 0x02, 0x00, 0x04]))
        assert rc == 4


class TestParsePublish:
    def test_simple_json_payload(self) -> None:
        # Build a PUBLISH packet manually:
        # topic = "t" (1 byte), payload = "{}" (2 bytes)
        topic_bytes = b"t"
        payload_bytes = b'{"key":"val"}'
        topic_len = len(topic_bytes).to_bytes(2, "big")
        remaining = topic_len + topic_bytes + payload_bytes
        pkt = bytes([0x30, len(remaining)]) + remaining

        result = parse_publish(pkt)
        assert isinstance(result, MQTTPublish)
        assert result.topic == "t"
        assert result.payload == b'{"key":"val"}'

    def test_longer_topic(self) -> None:
        topic = "d/CLOUD123/out/cameras/CAM456/is"
        payload = b'{"motionDetected":true}'
        topic_bytes = topic.encode()
        topic_len = len(topic_bytes).to_bytes(2, "big")
        remaining = topic_len + topic_bytes + payload
        pkt = bytes([0x30, len(remaining)]) + remaining

        result = parse_publish(pkt)
        assert result.topic == topic
        assert result.payload == payload

    def test_qos1_skips_packet_id(self) -> None:
        topic = "t"
        payload = b'{"x":1}'
        topic_bytes = topic.encode()
        topic_len = len(topic_bytes).to_bytes(2, "big")
        packet_id = b"\x00\x01"
        remaining = topic_len + topic_bytes + packet_id + payload
        # QoS 1: first byte has bits 1-2 set to 01 -> 0x32
        pkt = bytes([0x32, len(remaining)]) + remaining

        result = parse_publish(pkt)
        assert result.topic == "t"
        assert result.payload == payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mqtt_codec.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# eisenberg/mqtt_codec.py
"""Raw MQTT 3.1.1 packet construction and parsing.

Only implements the subset needed for Arlo:
CONNECT, CONNACK, SUBSCRIBE, SUBACK, PUBLISH, PINGREQ, PINGRESP, DISCONNECT.

No external MQTT library needed — the binary protocol is straightforward.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class MQTTPublish:
    """Parsed MQTT PUBLISH packet."""

    topic: str
    payload: bytes


def _encode_remaining_length(length: int) -> bytes:
    """Encode MQTT remaining length (variable-length encoding)."""
    result = bytearray()
    while True:
        byte = length & 0x7F
        length >>= 7
        if length > 0:
            byte |= 0x80
        result.append(byte)
        if length == 0:
            break
    return bytes(result)


def _decode_remaining_length(data: bytes, start: int) -> tuple[int, int]:
    """Decode MQTT remaining length. Returns (length, next_index)."""
    idx = start
    remaining = 0
    multiplier = 1
    while idx < len(data):
        byte = data[idx]
        remaining += (byte & 0x7F) * multiplier
        multiplier *= 128
        idx += 1
        if (byte & 0x80) == 0:
            break
    return remaining, idx


def _encode_utf8_string(s: str) -> bytes:
    """Encode a UTF-8 string with 2-byte length prefix."""
    encoded = s.encode("utf-8")
    return struct.pack("!H", len(encoded)) + encoded


def build_connect(
    client_id: str,
    username: str,
    password: str,
    keepalive: int,
) -> bytes:
    """Build an MQTT CONNECT packet."""
    # Variable header
    var_header = (
        b"\x00\x04MQTT"  # Protocol name
        b"\x04"  # Protocol level (MQTT 3.1.1)
        b"\xc2"  # Connect flags: username + password + clean session
        + struct.pack("!H", keepalive)
    )

    # Payload
    payload = (
        _encode_utf8_string(client_id)
        + _encode_utf8_string(username)
        + _encode_utf8_string(password)
    )

    remaining = var_header + payload
    return (
        bytes([0x10])
        + _encode_remaining_length(len(remaining))
        + remaining
    )


def build_subscribe(packet_id: int, topics: list[str]) -> bytes:
    """Build an MQTT SUBSCRIBE packet."""
    payload = struct.pack("!H", packet_id)
    for topic in topics:
        payload += _encode_utf8_string(topic) + b"\x00"  # QoS 0

    return (
        bytes([0x82])  # SUBSCRIBE with reserved bits
        + _encode_remaining_length(len(payload))
        + payload
    )


def build_pingreq() -> bytes:
    """Build an MQTT PINGREQ packet."""
    return bytes([0xC0, 0x00])


def build_disconnect() -> bytes:
    """Build an MQTT DISCONNECT packet."""
    return bytes([0xE0, 0x00])


def parse_packet_type(data: bytes) -> int:
    """Extract packet type from first byte (upper 4 bits)."""
    return (data[0] >> 4) & 0x0F


def parse_connack(data: bytes) -> int:
    """Parse CONNACK packet, return the return code (0 = success)."""
    # Byte 0: packet type, byte 1: remaining length
    # Byte 2: connect acknowledge flags, byte 3: return code
    return data[3]


def parse_publish(data: bytes) -> MQTTPublish:
    """Parse an MQTT PUBLISH packet into topic + payload."""
    _, idx = _decode_remaining_length(data, 1)

    # Topic length (2 bytes big-endian)
    topic_len = struct.unpack("!H", data[idx : idx + 2])[0]
    idx += 2

    topic = data[idx : idx + topic_len].decode("utf-8")
    idx += topic_len

    # QoS: bits 1-2 of first byte
    qos = (data[0] >> 1) & 0x03
    if qos > 0:
        idx += 2  # Skip packet ID

    payload = data[idx:]
    return MQTTPublish(topic=topic, payload=payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mqtt_codec.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add eisenberg/mqtt_codec.py tests/test_mqtt_codec.py
git commit -m "feat: add raw MQTT 3.1.1 packet codec"
```

---

## Task 4: Auth Client

**Files:**
- Create: `eisenberg/client.py`
- Test: `tests/test_client_auth.py`

The `EisenbergClient` handles authentication (both first-time push and trusted browser flows), session management, and REST API calls. Takes an `aiohttp.ClientSession` via constructor injection.

This task covers auth only. REST commands (snapshot, stream, etc.) are added in Task 5.

- [ ] **Step 1: Write auth tests**

```python
# tests/test_client_auth.py
"""Tests for EisenbergClient authentication flows."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from aiohttp import CookieJar
from aioresponses import aioresponses

from eisenberg.client import EisenbergClient
from eisenberg.exceptions import AuthenticationError, PushApprovalRequired


OCAPI = "https://ocapi-app.arlo.com"
MYAPI = "https://myapi.arlo.com"


def make_client(cookie_jar: CookieJar | None = None) -> EisenbergClient:
    return EisenbergClient(
        email="test@example.com",
        password="hunter2",
        device_id="test-device-uuid",
        cookie_jar=cookie_jar,
    )


class TestTrustedBrowserFlow:
    @pytest.fixture
    def mocked(self) -> aioresponses:
        with aioresponses() as m:
            # Step 1: auth
            m.post(f"{OCAPI}/api/auth", payload={
                "data": {
                    "token": "initial-token",
                    "userId": "USER-123",
                    "authCompleted": False,
                },
                "meta": {"code": 200},
            })
            # Step 2: getFactorId succeeds (browser trusted)
            m.post(f"{OCAPI}/api/getFactorId", payload={
                "data": {"factorId": "factor-abc"},
                "meta": {"code": 200},
            })
            # Step 3: startAuth completes instantly
            m.post(f"{OCAPI}/api/startAuth", payload={
                "data": {
                    "authCompleted": True,
                    "accessToken": {"token": "final-token"},
                },
                "meta": {"code": 200},
            })
            # Step 4: session
            m.get(f"{MYAPI}/hmsweb/users/session/v3", payload={
                "data": {"mqttUrl": "wss://mqtt.arlo.com:8084"},
                "success": True,
            })
            yield m

    async def test_trusted_login_returns_token(self, mocked: aioresponses) -> None:
        client = make_client()
        async with client:
            await client.login()
            assert client.token == "final-token"
            assert client.user_id == "USER-123"
            assert client.mqtt_url == "wss://mqtt.arlo.com:8084"

    async def test_trusted_login_no_push_needed(self, mocked: aioresponses) -> None:
        client = make_client()
        async with client:
            # Should NOT raise PushApprovalRequired
            await client.login()


class TestFirstTimeFlow:
    async def test_raises_push_approval_required(self) -> None:
        with aioresponses() as m:
            # Step 1: auth
            m.post(f"{OCAPI}/api/auth", payload={
                "data": {
                    "token": "initial-token",
                    "userId": "USER-123",
                    "authCompleted": False,
                },
                "meta": {"code": 200},
            })
            # Step 2: getFactorId FAILS (not trusted)
            m.post(f"{OCAPI}/api/getFactorId", payload={
                "data": {},
                "meta": {"code": 400, "error": "4012"},
            })
            # Step 3: startAuth returns factors
            m.post(f"{OCAPI}/api/startAuth", payload={
                "data": {
                    "factorAuthCode": "auth-code-xyz",
                    "factors": [
                        {"factorType": "PUSH", "displayName": "iPhone"},
                    ],
                    "MFA_Config": {"timeout": {"PUSH": 120}},
                },
                "meta": {"code": 200},
            })

            client = make_client()
            async with client:
                with pytest.raises(PushApprovalRequired) as exc_info:
                    await client.login()

                assert exc_info.value.factor_auth_code == "auth-code-xyz"
                assert len(exc_info.value.factors) == 1


class TestAuthFailure:
    async def test_bad_credentials_raises(self) -> None:
        with aioresponses() as m:
            m.post(f"{OCAPI}/api/auth", payload={
                "data": {},
                "meta": {"code": 401, "error": "1006"},
            })

            client = make_client()
            async with client:
                with pytest.raises(AuthenticationError):
                    await client.login()


class TestHeaders:
    def test_ocapi_headers_encode_token(self) -> None:
        client = make_client()
        headers = client._ocapi_headers("my-token")
        expected = base64.b64encode(b"my-token").decode()
        assert headers["Authorization"] == expected

    def test_myapi_headers_raw_token(self) -> None:
        client = make_client()
        client._x_cloud_id = "cloud-123"
        headers = client._myapi_headers("my-token")
        assert headers["Authorization"] == "my-token"
        assert headers["xCloudId"] == "cloud-123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_client_auth.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the auth client**

```python
# eisenberg/client.py
"""Arlo API client.

Typed async client for the Arlo REST API. Handles authentication
(both first-time push and trusted browser flows), session management,
and REST commands. Takes an aiohttp ClientSession via constructor
injection, or creates its own.

Cookie persistence for the browser trust cookie is handled externally
(by the HA integration config entry).
"""

from __future__ import annotations

import base64
import logging
import struct
import time
from types import TracebackType
from typing import Any

import aiohttp
from aiohttp import ClientSession, CookieJar

from .exceptions import (
    APIError,
    AuthenticationError,
    PushApprovalRequired,
    SessionExpiredError,
)
from .models import DeviceInfo, StreamResponse

_LOGGER = logging.getLogger(__name__)

OCAPI_BASE = "https://ocapi-app.arlo.com"
MYAPI_BASE = "https://myapi.arlo.com"

# Mobile UA to get RTSP URLs from startStream (not DASH)
_MOBILE_UA = "Arlo/4.0 (iPhone; iOS 18.0)"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


class EisenbergClient:
    """Async client for the Arlo camera API."""

    def __init__(
        self,
        email: str,
        password: str,
        device_id: str,
        cookie_jar: CookieJar | None = None,
        http_session: ClientSession | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._device_id = device_id
        self._cookie_jar = cookie_jar or CookieJar(unsafe=True)
        self._external_session = http_session is not None
        self._session = http_session
        self._owns_session = False

        self.token: str | None = None
        self.user_id: str | None = None
        self.mqtt_url: str | None = None
        self._x_cloud_id: str | None = None
        self._token_issued_at: float = 0

    async def __aenter__(self) -> EisenbergClient:
        if self._session is None:
            self._session = ClientSession(cookie_jar=self._cookie_jar)
            self._owns_session = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("Client not initialized. Use async with.")
        return self._session

    @property
    def x_cloud_id(self) -> str:
        if self._x_cloud_id is None:
            raise RuntimeError("x_cloud_id not set. Call get_devices() first.")
        return self._x_cloud_id

    def _ocapi_headers(self, token: str | None = None) -> dict[str, str]:
        """Headers for ocapi-app.arlo.com requests."""
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Source": "arloCamWeb",
            "Auth-Version": "2",
            "X-User-Device-Id": self._device_id,
            "X-User-Device-Type": "BROWSER",
            "X-User-Device-Automation-Name": base64.b64encode(
                b"BROWSER"
            ).decode(),
            "X-Service-Version": "v3",
            "Origin": "https://my.arlo.com",
            "Referer": "https://my.arlo.com/",
            "User-Agent": _BROWSER_UA,
        }
        if token:
            headers["Authorization"] = base64.b64encode(
                token.encode()
            ).decode()
        return headers

    def _myapi_headers(self, token: str) -> dict[str, str]:
        """Headers for myapi.arlo.com — raw token, not base64."""
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Authorization": token,
            "Auth-Version": "2",
            "xCloudId": self._x_cloud_id or "",
            "Origin": "https://my.arlo.com",
            "Referer": "https://my.arlo.com/",
            "User-Agent": _BROWSER_UA,
        }

    def _myapi_headers_mobile(self, token: str) -> dict[str, str]:
        """Headers for myapi.arlo.com with mobile UA (for RTSP streams)."""
        headers = self._myapi_headers(token)
        headers["User-Agent"] = _MOBILE_UA
        headers["x-user-device-type"] = "PHONE"
        return headers

    async def login(self) -> None:
        """Full auth flow. Sets self.token, self.user_id, self.mqtt_url.

        Raises PushApprovalRequired if this is the first login and the
        browser trust cookie is missing. The caller (config flow) should
        show a UI step asking the user to approve the push, then call
        complete_push_approval().

        Raises AuthenticationError on bad credentials.
        """
        password_b64 = base64.b64encode(self._password.encode()).decode()

        # Step 1: Initial auth
        async with self.session.post(
            f"{OCAPI_BASE}/api/auth",
            headers=self._ocapi_headers(),
            json={
                "email": self._email,
                "password": password_b64,
                "language": "en",
                "EnvSource": "prod",
            },
        ) as resp:
            body = await resp.json()

        if body["meta"]["code"] != 200:
            raise AuthenticationError(
                f"Auth failed: {body['meta'].get('error', 'unknown')}"
            )

        auth_data = body["data"]
        token = auth_data["token"]
        self.user_id = auth_data["userId"]

        if auth_data.get("authCompleted"):
            self.token = token
            self._token_issued_at = time.monotonic()
            await self._establish_session()
            return

        # Step 2: Check browser trust
        async with self.session.post(
            f"{OCAPI_BASE}/api/getFactorId",
            headers=self._ocapi_headers(token),
            json={
                "factorType": "BROWSER",
                "factorData": "",
                "userId": self.user_id,
            },
        ) as resp:
            body = await resp.json()

        if body["meta"]["code"] == 200:
            # Browser trusted — instant auth with factorId
            factor_id = body["data"]["factorId"]
            async with self.session.post(
                f"{OCAPI_BASE}/api/startAuth",
                headers=self._ocapi_headers(token),
                json={
                    "factorId": factor_id,
                    "factorType": "BROWSER",
                    "userId": self.user_id,
                },
            ) as resp:
                body = await resp.json()

            if body["meta"]["code"] != 200:
                raise AuthenticationError(
                    f"Trusted startAuth failed: {body['meta'].get('error')}"
                )

            start_data = body["data"]
            if not start_data.get("authCompleted"):
                raise AuthenticationError(
                    "Trusted browser auth did not auto-complete"
                )

            self.token = start_data["accessToken"]["token"]
            self._token_issued_at = time.monotonic()
            await self._establish_session()
            return

        # Browser not trusted — first-time flow, need push approval
        _LOGGER.info("Browser not trusted, initiating push approval")
        async with self.session.post(
            f"{OCAPI_BASE}/api/startAuth",
            headers=self._ocapi_headers(token),
            json={"factorType": "", "userId": self.user_id},
        ) as resp:
            body = await resp.json()

        if body["meta"]["code"] != 200:
            raise AuthenticationError(
                f"startAuth failed: {body['meta'].get('error')}"
            )

        start_data = body["data"]
        # Store token for use in complete_push_approval
        self.token = token
        raise PushApprovalRequired(
            factor_auth_code=start_data["factorAuthCode"],
            factors=start_data["factors"],
        )

    async def complete_push_approval(
        self,
        factor_auth_code: str,
        timeout: int = 120,
        poll_interval: int = 3,
    ) -> None:
        """Poll finishAuth until user approves push, then establish trust.

        Called after login() raises PushApprovalRequired.
        """
        import asyncio

        elapsed = 0
        while elapsed < timeout:
            async with self.session.post(
                f"{OCAPI_BASE}/api/finishAuth",
                headers=self._ocapi_headers(self.token),
                json={
                    "factorAuthCode": factor_auth_code,
                    "isBrowserTrusted": True,
                },
            ) as resp:
                body = await resp.json()

            if (
                body["meta"]["code"] == 200
                and body["data"].get("authCompleted")
            ):
                finish_data = body["data"]
                self.token = finish_data["token"]
                self._token_issued_at = time.monotonic()

                # Trust this browser
                browser_auth_code = finish_data.get("browserAuthCode")
                if browser_auth_code:
                    await self._pair_browser(browser_auth_code)

                await self._establish_session()
                return

            elapsed += poll_interval
            await asyncio.sleep(poll_interval)

        raise AuthenticationError(
            f"Push approval not received within {timeout}s"
        )

    async def _pair_browser(self, browser_auth_code: str) -> None:
        """Register this browser as trusted (sets 14-day cookie)."""
        async with self.session.post(
            f"{OCAPI_BASE}/api/startPairingFactor",
            headers=self._ocapi_headers(self.token),
            json={
                "factorType": "BROWSER",
                "factorData": "",
                "factorAuthCode": browser_auth_code,
            },
        ) as resp:
            body = await resp.json()

        if body["meta"]["code"] != 200:
            _LOGGER.warning("Failed to pair browser: %s", body)

    async def _establish_session(self) -> None:
        """Get session info (MQTT URL) from myapi."""
        if self.token is None:
            raise RuntimeError("Cannot establish session without token")

        async with self.session.get(
            f"{MYAPI_BASE}/hmsweb/users/session/v3",
            headers=self._myapi_headers(self.token),
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Session establishment failed",
            )

        self.mqtt_url = body["data"].get("mqttUrl", "")

    def token_needs_refresh(self) -> bool:
        """Check if token is close to expiry (~2hr lifetime, refresh at 90min)."""
        if self.token is None:
            return True
        elapsed = time.monotonic() - self._token_issued_at
        return elapsed > 5400  # 90 minutes

    async def get_devices(self) -> list[DeviceInfo]:
        """Fetch all devices. Sets x_cloud_id from first camera found."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        async with self.session.get(
            f"{MYAPI_BASE}/hmsweb/v2/users/devices",
            headers=self._myapi_headers(self.token),
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Failed to list devices",
            )

        devices = [DeviceInfo.model_validate(d) for d in body["data"]]

        # Set xCloudId from first device
        if devices and self._x_cloud_id is None:
            self._x_cloud_id = devices[0].x_cloud_id

        return devices

    async def request_snapshot(self, device_id: str) -> None:
        """Request a full-frame snapshot. Response comes via MQTT."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        async with self.session.post(
            f"{MYAPI_BASE}/hmsweb/users/devices/notify/{device_id}",
            headers=self._myapi_headers(self.token),
            json={
                "from": f"{self.user_id}_web",
                "to": device_id,
                "action": "set",
                "resource": f"cameras/{device_id}",
                "publishResponse": True,
                "properties": {"activityState": "fullFrameSnapshot"},
                "transId": f"web!snapshot!{int(time.time())}",
            },
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Snapshot request failed",
            )

    async def start_stream(self, device_id: str) -> StreamResponse:
        """Start a live stream. Returns RTSP URL (uses mobile UA)."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        async with self.session.post(
            f"{MYAPI_BASE}/hmsweb/users/devices/startStream",
            headers=self._myapi_headers_mobile(self.token),
            json={
                "from": f"{self.user_id}_web",
                "to": device_id,
                "action": "set",
                "resource": f"cameras/{device_id}",
                "publishResponse": True,
                "transId": f"web!stream!{int(time.time())}",
                "properties": {
                    "activityState": "startUserStream",
                    "cameraId": device_id,
                },
            },
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Stream start failed",
            )

        return StreamResponse.model_validate(body["data"])

    async def set_siren(self, device_id: str, *, on: bool) -> None:
        """Turn siren on or off."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        properties: dict[str, Any] = {
            "sirenState": "on" if on else "off",
        }
        if on:
            properties["duration"] = 180
            properties["volume"] = 8
            properties["pattern"] = "alarm"

        async with self.session.post(
            f"{MYAPI_BASE}/hmsweb/users/devices/notify/{device_id}",
            headers=self._myapi_headers(self.token),
            json={
                "from": f"{self.user_id}_web",
                "to": device_id,
                "action": "set",
                "resource": f"siren/{device_id}",
                "publishResponse": True,
                "transId": f"web!siren!{int(time.time())}",
                "properties": properties,
            },
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Siren command failed",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_client_auth.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add eisenberg/client.py tests/test_client_auth.py
git commit -m "feat: add EisenbergClient with auth flows and REST commands"
```

---

## Task 5: MQTT Event Stream

**Files:**
- Create: `eisenberg/mqtt.py`
- Test: `tests/test_mqtt.py`

The `MQTTEventStream` manages the WebSocket connection, MQTT protocol, and dispatches parsed events to registered callbacks.

- [ ] **Step 1: Write tests**

```python
# tests/test_mqtt.py
"""Tests for MQTT event stream dispatcher."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from eisenberg.mqtt import MQTTEventStream, TopicRouter


class TestTopicRouter:
    def test_register_and_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/cameras/+/is", handler)
        matched = router.match("d/CLOUD/out/cameras/CAM123/is")
        assert matched == [handler]

    def test_wildcard_hash(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/#", handler)
        matched = router.match("d/CLOUD/out/cameras/CAM/is")
        assert matched == [handler]

    def test_no_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/cameras/+/is", handler)
        matched = router.match("u/USER/in/feed/live")
        assert matched == []

    def test_multiple_handlers(self) -> None:
        router = TopicRouter()
        h1 = MagicMock()
        h2 = MagicMock()
        router.register("d/+/out/#", h1)
        router.register("d/+/out/cameras/+/is", h2)
        matched = router.match("d/CLOUD/out/cameras/CAM/is")
        assert h1 in matched
        assert h2 in matched

    def test_exact_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("u/USER/in/feed/live", handler)
        matched = router.match("u/USER/in/feed/live")
        assert matched == [handler]
        assert router.match("u/USER/in/feed/other") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mqtt.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# eisenberg/mqtt.py
"""MQTT event stream over WebSocket.

Manages the persistent WebSocket connection to Arlo's MQTT broker,
handles MQTT protocol packets, and dispatches parsed PUBLISH messages
to registered topic handlers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from .mqtt_codec import (
    build_connect,
    build_disconnect,
    build_pingreq,
    build_subscribe,
    parse_connack,
    parse_packet_type,
    parse_publish,
)

_LOGGER = logging.getLogger(__name__)

# MQTT packet types
CONNACK = 2
PUBLISH = 3
SUBACK = 9
PINGRESP = 13

# Handler type: async callback(topic, payload_dict)
EventHandler = Callable[[str, dict[str, Any]], Any]


class TopicRouter:
    """Routes MQTT topics to registered handlers.

    Supports MQTT-style wildcards:
    - `+` matches exactly one level
    - `#` matches zero or more levels (must be last)
    """

    def __init__(self) -> None:
        self._routes: list[tuple[list[str], EventHandler]] = []

    def register(self, pattern: str, handler: EventHandler) -> None:
        """Register a handler for a topic pattern."""
        self._routes.append((pattern.split("/"), handler))

    def match(self, topic: str) -> list[EventHandler]:
        """Find all handlers matching a topic."""
        parts = topic.split("/")
        matched = []
        for pattern_parts, handler in self._routes:
            if self._matches(pattern_parts, parts):
                matched.append(handler)
        return matched

    @staticmethod
    def _matches(pattern: list[str], topic: list[str]) -> bool:
        for i, p in enumerate(pattern):
            if p == "#":
                return True  # Matches rest
            if i >= len(topic):
                return False
            if p != "+" and p != topic[i]:
                return False
        return len(pattern) == len(topic)


class MQTTEventStream:
    """Persistent MQTT connection over WebSocket to Arlo's broker.

    Usage:
        stream = MQTTEventStream(mqtt_url, user_id, token, x_cloud_id)
        stream.on("d/+/out/cameras/+/is", handle_camera_state)
        await stream.connect()
        # ... runs until disconnect
        await stream.disconnect()
    """

    def __init__(
        self,
        mqtt_url: str,
        user_id: str,
        token: str,
        x_cloud_id: str,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._mqtt_url = mqtt_url
        self._user_id = user_id
        self._token = token
        self._x_cloud_id = x_cloud_id
        self._session = http_session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._router = TopicRouter()
        self._running = False
        self._listen_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._on_disconnect: Callable[[], Any] | None = None

    def on(self, topic_pattern: str, handler: EventHandler) -> None:
        """Register a handler for a topic pattern."""
        self._router.register(topic_pattern, handler)

    def on_disconnect(self, callback: Callable[[], Any]) -> None:
        """Register a callback for when the connection drops."""
        self._on_disconnect = callback

    async def connect(self) -> None:
        """Connect to MQTT broker, subscribe, and start listening."""
        owns_session = False
        if self._session is None:
            self._session = aiohttp.ClientSession()
            owns_session = True

        try:
            self._ws = await self._session.ws_connect(
                f"{self._mqtt_url}/mqtt",
                protocols=["mqtt"],
                headers={
                    "Origin": "https://my.arlo.com",
                    "User-Agent": "Mozilla/5.0",
                },
            )
        except Exception:
            if owns_session:
                await self._session.close()
            raise

        # MQTT CONNECT
        client_id = f"user_{self._user_id}_{int(asyncio.get_event_loop().time())}"
        connect_pkt = build_connect(
            client_id=client_id,
            username=self._user_id,
            password=self._token,
            keepalive=60,
        )
        await self._ws.send_bytes(connect_pkt)

        # Wait for CONNACK
        msg = await self._ws.receive()
        data = msg.data if isinstance(msg.data, bytes) else msg.data.encode()
        rc = parse_connack(data)
        if rc != 0:
            await self._ws.close()
            raise ConnectionError(f"MQTT CONNACK failed with rc={rc}")

        # SUBSCRIBE
        topics = [
            f"d/{self._x_cloud_id}/out/#",
            f"u/{self._user_id}/in/#",
        ]
        subscribe_pkt = build_subscribe(packet_id=1, topics=topics)
        await self._ws.send_bytes(subscribe_pkt)

        # Wait for SUBACK
        await self._ws.receive()

        _LOGGER.info("MQTT connected and subscribed to %s", topics)
        self._running = True

        # Start background tasks
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def disconnect(self) -> None:
        """Gracefully disconnect."""
        self._running = False

        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None

        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None

        if self._ws and not self._ws.closed:
            await self._ws.send_bytes(build_disconnect())
            await self._ws.close()
            self._ws = None

    async def _listen_loop(self) -> None:
        """Read MQTT packets and dispatch PUBLISH messages."""
        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=90)
            except asyncio.TimeoutError:
                _LOGGER.warning("MQTT receive timeout")
                break
            except asyncio.CancelledError:
                return

            if msg.type == aiohttp.WSMsgType.BINARY:
                data = msg.data
                pkt_type = parse_packet_type(data)

                if pkt_type == PUBLISH:
                    parsed = parse_publish(data)
                    try:
                        payload = json.loads(parsed.payload)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        _LOGGER.warning(
                            "Non-JSON MQTT payload on %s: %s",
                            parsed.topic,
                            parsed.payload[:200],
                        )
                        continue

                    handlers = self._router.match(parsed.topic)
                    if handlers:
                        for handler in handlers:
                            try:
                                result = handler(parsed.topic, payload)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception:
                                _LOGGER.exception(
                                    "Error in MQTT handler for %s",
                                    parsed.topic,
                                )
                    else:
                        _LOGGER.info(
                            "Unhandled MQTT topic %s: %s",
                            parsed.topic,
                            json.dumps(payload)[:500],
                        )
                elif pkt_type == PINGRESP:
                    pass  # Expected keepalive response
                else:
                    _LOGGER.debug("MQTT packet type %d", pkt_type)

            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                _LOGGER.warning("MQTT WebSocket closed/error")
                break

        self._running = False
        if self._on_disconnect:
            result = self._on_disconnect()
            if asyncio.iscoroutine(result):
                await result

    async def _keepalive_loop(self) -> None:
        """Send PINGREQ every 50 seconds (keepalive is 60s)."""
        try:
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(50)
                if self._ws and not self._ws.closed:
                    await self._ws.send_bytes(build_pingreq())
        except asyncio.CancelledError:
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mqtt.py -v`
Expected: all tests PASS

- [ ] **Step 5: Update exports**

Add to `eisenberg/__init__.py`:

```python
from .mqtt import MQTTEventStream
```

And add `"MQTTEventStream"` to `__all__`.

- [ ] **Step 6: Commit**

```bash
git add eisenberg/mqtt.py tests/test_mqtt.py eisenberg/__init__.py
git commit -m "feat: add MQTT event stream with topic routing"
```

---

## Task 6: HA Integration Constants and Config

**Files:**
- Create: `custom_components/eisenberg/const.py`
- Modify: `custom_components/eisenberg/manifest.json`
- Create: `custom_components/eisenberg/translations/en.json`

- [ ] **Step 1: Write constants**

```python
# custom_components/eisenberg/const.py
"""Constants for the Eisenberg integration."""

DOMAIN = "eisenberg"

CONF_DEVICE_ID = "device_id"
CONF_TRUST_COOKIE = "trust_cookie"
CONF_MEDIA_DIR = "media_dir"
CONF_DETECTION_TIMEOUT = "detection_timeout"

DEFAULT_DETECTION_TIMEOUT = 30

EVENT_MEDIA = "eisenberg_media"
```

- [ ] **Step 2: Write translations**

```json
// custom_components/eisenberg/translations/en.json
{
  "config": {
    "step": {
      "user": {
        "title": "Arlo Account",
        "description": "Enter your Arlo account credentials.",
        "data": {
          "username": "Email",
          "password": "Password"
        }
      },
      "push_approval": {
        "title": "Push Approval",
        "description": "A push notification has been sent to your phone. Approve it in the Arlo app, then click Submit."
      },
      "media_storage": {
        "title": "Media Storage",
        "description": "Choose where to save motion clips and snapshots. Select 'Disabled' for no archival.",
        "data": {
          "media_dir": "Storage Location"
        }
      }
    },
    "error": {
      "invalid_auth": "Invalid email or password.",
      "push_timeout": "Push approval timed out. Try again.",
      "cannot_connect": "Cannot connect to Arlo servers."
    },
    "abort": {
      "already_configured": "This account is already configured."
    }
  },
  "options": {
    "step": {
      "init": {
        "data": {
          "media_dir": "Storage Location",
          "detection_timeout": "Detection sensor reset timeout (seconds)"
        }
      }
    }
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add custom_components/eisenberg/const.py custom_components/eisenberg/translations/
git commit -m "feat: add HA integration constants and translations"
```

---

## Task 7: HA Config Flow

**Files:**
- Create: `custom_components/eisenberg/config_flow.py`
- Test: `tests/test_config_flow.py`

Multi-step config flow: credentials -> push approval (if needed) -> media storage selection.

- [ ] **Step 1: Write config flow tests**

These tests need HA test infrastructure. Write focused tests for the happy path and key error cases.

```python
# tests/test_config_flow.py
"""Tests for the Eisenberg config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from eisenberg.exceptions import AuthenticationError, PushApprovalRequired


# These tests validate the config flow logic in isolation.
# Full HA integration tests require a running HA instance.


class TestConfigFlowLogic:
    """Test config flow decision logic without HA infrastructure."""

    async def test_trusted_login_skips_push(self) -> None:
        """When login succeeds (trusted browser), no push step needed."""
        client = AsyncMock()
        client.login = AsyncMock(return_value=None)
        client.token = "test-token"
        client.user_id = "USER-123"
        client.mqtt_url = "wss://mqtt.arlo.com"
        # If login() returns without raising, push is not needed
        await client.login()
        # No PushApprovalRequired raised = success

    async def test_first_time_login_requires_push(self) -> None:
        """When login raises PushApprovalRequired, need push step."""
        client = AsyncMock()
        client.login = AsyncMock(
            side_effect=PushApprovalRequired(
                factor_auth_code="code-123",
                factors=[{"factorType": "PUSH", "displayName": "Phone"}],
            )
        )
        with pytest.raises(PushApprovalRequired) as exc_info:
            await client.login()
        assert exc_info.value.factor_auth_code == "code-123"

    async def test_bad_credentials_raises_auth_error(self) -> None:
        """When credentials are wrong, raises AuthenticationError."""
        client = AsyncMock()
        client.login = AsyncMock(
            side_effect=AuthenticationError("Invalid credentials")
        )
        with pytest.raises(AuthenticationError):
            await client.login()
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_config_flow.py -v`
Expected: PASS

- [ ] **Step 3: Write config flow**

```python
# custom_components/eisenberg/config_flow.py
"""Config flow for Eisenberg."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from eisenberg import (
    AuthenticationError,
    EisenbergClient,
    PushApprovalRequired,
)

from .const import (
    CONF_DETECTION_TIMEOUT,
    CONF_DEVICE_ID,
    CONF_MEDIA_DIR,
    DEFAULT_DETECTION_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MEDIA_DIR_DISABLED = "__disabled__"


class EisenbergConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Eisenberg."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: EisenbergClient | None = None
        self._device_id: str = ""
        self._username: str = ""
        self._password: str = ""
        self._factor_auth_code: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Email and password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._device_id = f"eisenberg-{uuid.uuid4()}"

            self._client = EisenbergClient(
                email=self._username,
                password=self._password,
                device_id=self._device_id,
            )

            try:
                async with self._client:
                    await self._client.login()
                # Trusted browser — skip push
                return await self.async_step_media_storage()
            except PushApprovalRequired as err:
                self._factor_auth_code = err.factor_auth_code
                return await self.async_step_push_approval()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
                self._client = None
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "cannot_connect"
                self._client = None

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_push_approval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Wait for push approval."""
        errors: dict[str, str] = {}

        if user_input is not None and self._client is not None:
            try:
                async with self._client:
                    await self._client.complete_push_approval(
                        factor_auth_code=self._factor_auth_code,
                        timeout=120,
                    )
                return await self.async_step_media_storage()
            except AuthenticationError:
                errors["base"] = "push_timeout"

        return self.async_show_form(
            step_id="push_approval",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_media_storage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Select media storage location."""
        if user_input is not None:
            media_dir = user_input.get(CONF_MEDIA_DIR, MEDIA_DIR_DISABLED)

            await self.async_set_unique_id(self._username)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=self._username,
                data={
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    CONF_DEVICE_ID: self._device_id,
                },
                options={
                    CONF_MEDIA_DIR: media_dir
                    if media_dir != MEDIA_DIR_DISABLED
                    else "",
                    CONF_DETECTION_TIMEOUT: DEFAULT_DETECTION_TIMEOUT,
                },
            )

        # Build media dir options from HA config
        media_dirs = self.hass.config.media_dirs
        options = {MEDIA_DIR_DISABLED: "Disabled"}
        for name, path in media_dirs.items():
            options[name] = f"{name} ({path})"

        return self.async_show_form(
            step_id="media_storage",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_MEDIA_DIR, default=MEDIA_DIR_DISABLED
                ): vol.In(options),
            }),
        )

    # --- Reauth ---

    async def async_step_reauth(
        self, entry_data: dict[str, str]
    ) -> ConfigFlowResult:
        """Handle reauth triggered by ConfigEntryAuthFailed."""
        self._username = entry_data[CONF_USERNAME]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm new credentials for reauth."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._device_id = entry.data.get(
                CONF_DEVICE_ID, f"eisenberg-{uuid.uuid4()}"
            )

            self._client = EisenbergClient(
                email=self._username,
                password=self._password,
                device_id=self._device_id,
            )

            try:
                async with self._client:
                    await self._client.login()
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_DEVICE_ID: self._device_id,
                    },
                )
            except PushApprovalRequired as err:
                self._factor_auth_code = err.factor_auth_code
                return await self.async_step_reauth_push()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
                self._client = None

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, default=self._username): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_reauth_push(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth: wait for push approval."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None and self._client is not None:
            try:
                async with self._client:
                    await self._client.complete_push_approval(
                        factor_auth_code=self._factor_auth_code,
                    )
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_DEVICE_ID: self._device_id,
                    },
                )
            except AuthenticationError:
                errors["base"] = "push_timeout"

        return self.async_show_form(
            step_id="reauth_push",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        return EisenbergOptionsFlow()


class EisenbergOptionsFlow(OptionsFlow):
    """Options flow for Eisenberg."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        media_dirs = self.hass.config.media_dirs
        options = {"": "Disabled"}
        for name, path in media_dirs.items():
            options[name] = f"{name} ({path})"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_MEDIA_DIR,
                    default=opts.get(CONF_MEDIA_DIR, ""),
                ): vol.In(options),
                vol.Required(
                    CONF_DETECTION_TIMEOUT,
                    default=opts.get(
                        CONF_DETECTION_TIMEOUT, DEFAULT_DETECTION_TIMEOUT
                    ),
                ): vol.All(int, vol.Range(min=5, max=300)),
            }),
        )
```

- [ ] **Step 4: Commit**

```bash
git add custom_components/eisenberg/config_flow.py tests/test_config_flow.py
git commit -m "feat: add config flow with push approval and media storage"
```

---

## Task 8: HA Coordinator

**Files:**
- Create: `custom_components/eisenberg/coordinator.py`
- Modify: `custom_components/eisenberg/__init__.py`

The coordinator bridges MQTT events to HA entities. Event-driven, not polling.

- [ ] **Step 1: Write coordinator**

This is the central orchestrator. It owns the client, manages MQTT, and updates entity state. Due to deep HA integration, this is tested via integration tests, not unit tests.

```python
# custom_components/eisenberg/coordinator.py
"""Event-driven coordinator for Eisenberg.

Unlike a typical DataUpdateCoordinator that polls, this coordinator
listens to MQTT events and updates entity state in real-time. The
periodic update is only used for health checks (token refresh, device
list sync).
"""

from __future__ import annotations

import asyncio
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
    SessionExpiredError,
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
    DEFAULT_DETECTION_TIMEOUT,
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

    async def _async_setup(self) -> None:
        """Initialize client and MQTT on first refresh."""
        self._http_session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True)
        )
        self.client._session = self._http_session
        self.client._owns_session = False

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
        self._mqtt.on(
            "u/+/in/automation/activeMode/is", self._handle_active_mode
        )

        # Connectivity
        self._mqtt.on(
            "d/+/out/basestation/connectivity/is",
            self._handle_connectivity,
        )

        # Reconnect handler
        self._mqtt.on_disconnect(self._handle_mqtt_disconnect)

    async def _handle_camera_state(
        self, topic: str, payload: dict[str, Any]
    ) -> None:
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

    async def _handle_snapshot(
        self, topic: str, payload: dict[str, Any]
    ) -> None:
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
            await self._archive_media(
                device_id, snap.presigned_url, "snapshot", "jpg"
            )

            self.async_set_updated_data(self.data or {})
        except Exception:
            _LOGGER.warning(
                "Failed to parse snapshot for %s: %s",
                device_id,
                json.dumps(payload)[:500],
            )

    async def _handle_siren(
        self, topic: str, payload: dict[str, Any]
    ) -> None:
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

    async def _handle_feed(
        self, topic: str, payload: dict[str, Any]
    ) -> None:
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
                    self.latest_thumbnails[event.device_id] = (
                        event.thumbnail_url
                    )

                # Fire HA event
                self.hass.bus.async_fire(EVENT_MEDIA, {
                    "device_id": event.device_id,
                    "type": "motion",
                    "category": event.obj_category,
                    "categories": event.obj_categories,
                    "content_url": event.content_url,
                    "thumbnail_url": event.thumbnail_url,
                    "duration": event.duration,
                    "timestamp": event.utc_created_date,
                })

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

    async def _handle_media_upload(
        self, topic: str, payload: dict[str, Any]
    ) -> None:
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

    async def _handle_active_mode(
        self, topic: str, payload: dict[str, Any]
    ) -> None:
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

    async def _handle_connectivity(
        self, topic: str, payload: dict[str, Any]
    ) -> None:
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
                    _LOGGER.warning(
                        "Failed to download %s: HTTP %d", url, resp.status
                    )
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
                if (
                    self.client.user_id
                    and self.client.token
                    and self._http_session
                ):
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
```

- [ ] **Step 2: Write integration setup/teardown**

```python
# custom_components/eisenberg/__init__.py
"""Eisenberg camera integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)

type EisenbergConfigEntry = ConfigEntry[EisenbergCoordinator]

PLATFORMS = ["camera", "binary_sensor", "sensor", "switch"]


async def async_setup_entry(
    hass: HomeAssistant, entry: EisenbergConfigEntry
) -> bool:
    """Set up Eisenberg from a config entry."""
    coordinator = EisenbergCoordinator(hass, entry)
    await coordinator._async_setup()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: EisenbergConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        await entry.runtime_data.async_shutdown()

    return unload_ok
```

- [ ] **Step 3: Commit**

```bash
git add custom_components/eisenberg/coordinator.py custom_components/eisenberg/__init__.py
git commit -m "feat: add event-driven coordinator with MQTT handlers and media archival"
```

---

## Task 9: Camera Entity

**Files:**
- Create: `custom_components/eisenberg/camera.py`

- [ ] **Step 1: Write camera entity**

```python
# custom_components/eisenberg/camera.py
"""Camera platform for Eisenberg."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Eisenberg cameras."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(
        EisenbergCamera(coordinator, device)
        for device in coordinator.devices
    )


class EisenbergCamera(CoordinatorEntity[EisenbergCoordinator], Camera):
    """Arlo camera entity with snapshot and RTSP stream support."""

    _attr_has_entity_name = True
    _attr_name = None  # Use device name

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: "from eisenberg import DeviceInfo",
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)

        self._device = device
        self._attr_unique_id = f"{device.device_id}_camera"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
            "name": device.device_name,
            "manufacturer": "Arlo",
            "model": device.model_id,
        }

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest camera image."""
        # Try latest thumbnail from motion event first
        url = self.coordinator.latest_thumbnails.get(self._device.device_id)
        if not url:
            # Try latest snapshot
            url = self.coordinator.latest_snapshots.get(
                self._device.device_id
            )
        if not url:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception:
            _LOGGER.debug("Failed to fetch camera image from %s", url)

        return None

    async def stream_source(self) -> str | None:
        """Return the RTSP stream source URL."""
        try:
            resp = await self.coordinator.client.start_stream(
                self._device.device_id
            )
            return resp.url
        except Exception:
            _LOGGER.exception("Failed to start stream for %s", self._device.device_id)
            return None

    @property
    def is_streaming(self) -> bool:
        """Return whether the camera is streaming."""
        state = self.coordinator.device_states.get(self._device.device_id)
        if state and state.activity_state:
            return state.activity_state in (
                "userStreamActive",
                "alertStreamActive",
            )
        return False

    @property
    def motion_detection_enabled(self) -> bool:
        """Return whether motion detection is enabled."""
        return self.coordinator.active_mode != "standby"
```

- [ ] **Step 2: Commit**

```bash
git add custom_components/eisenberg/camera.py
git commit -m "feat: add camera entity with snapshot and RTSP stream"
```

---

## Task 10: Binary Sensors

**Files:**
- Create: `custom_components/eisenberg/binary_sensor.py`

- [ ] **Step 1: Write binary sensor entities**

```python
# custom_components/eisenberg/binary_sensor.py
"""Binary sensor platform for Eisenberg.

Motion detected: from MQTT motionDetected property (resets via MQTT).
Person/Vehicle/Animal: from AI classification in feed/live events
(auto-resets after configurable timeout since MQTT only resets generic motion).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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
        entities.append(
            DetectionSensor(coordinator, device, "Person", entry)
        )
        entities.append(
            DetectionSensor(coordinator, device, "Vehicle", entry)
        )
        entities.append(
            DetectionSensor(coordinator, device, "Animal", entry)
        )

    async_add_entities(entities)


class MotionSensor(
    CoordinatorEntity[EisenbergCoordinator], BinarySensorEntity
):
    """Motion detected binary sensor — directly from MQTT motionDetected."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_name = "Motion"

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

    @property
    def is_on(self) -> bool | None:
        """Return True if motion is detected."""
        state = self.coordinator.device_states.get(self._device.device_id)
        if state is None:
            return None
        return state.motion_detected


class DetectionSensor(
    CoordinatorEntity[EisenbergCoordinator], BinarySensorEntity
):
    """AI detection binary sensor (person/vehicle/animal).

    Turns on when the AI classification matches, auto-resets after
    a configurable timeout since MQTT only sends motionDetected=false
    for generic motion, not per-category.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOTION

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
        self._is_on = False
        self._reset_task: asyncio.Task[None] | None = None
        self._attr_name = f"{category} detected"
        self._attr_unique_id = f"{device.device_id}_{category.lower()}"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @property
    def is_on(self) -> bool:
        return self._is_on

    @callback
    def _handle_coordinator_update(self) -> None:
        """Check if latest motion event matches our category."""
        event = self.coordinator.motion_events.get(self._device.device_id)
        if (
            event
            and event.obj_categories
            and self._category in event.obj_categories
            and not self._is_on
        ):
            self._is_on = True
            self._schedule_reset()
            self.async_write_ha_state()

    def _schedule_reset(self) -> None:
        """Schedule auto-reset after timeout."""
        if self._reset_task:
            self._reset_task.cancel()

        timeout = self._entry.options.get(
            CONF_DETECTION_TIMEOUT, DEFAULT_DETECTION_TIMEOUT
        )

        async def _reset() -> None:
            await asyncio.sleep(timeout)
            self._is_on = False
            self.async_write_ha_state()

        self._reset_task = self.hass.async_create_task(_reset())
```

- [ ] **Step 2: Commit**

```bash
git add custom_components/eisenberg/binary_sensor.py
git commit -m "feat: add motion and AI detection binary sensors"
```

---

## Task 11: Sensors and Siren Switch

**Files:**
- Create: `custom_components/eisenberg/sensor.py`
- Create: `custom_components/eisenberg/switch.py`

- [ ] **Step 1: Write sensor entities**

```python
# custom_components/eisenberg/sensor.py
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


class BatterySensor(
    CoordinatorEntity[EisenbergCoordinator], SensorEntity
):
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


class SignalStrengthSensor(
    CoordinatorEntity[EisenbergCoordinator], SensorEntity
):
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
```

- [ ] **Step 2: Write siren switch**

```python
# custom_components/eisenberg/switch.py
"""Switch platform for Eisenberg — siren control."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eisenberg import DeviceInfo

from .coordinator import EisenbergCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Eisenberg switches."""
    coordinator: EisenbergCoordinator = entry.runtime_data
    async_add_entities(
        SirenSwitch(coordinator, device) for device in coordinator.devices
    )


class SirenSwitch(
    CoordinatorEntity[EisenbergCoordinator], SwitchEntity
):
    """Siren on/off switch."""

    _attr_has_entity_name = True
    _attr_name = "Siren"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:alarm-light"

    def __init__(
        self,
        coordinator: EisenbergCoordinator,
        device: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.device_id}_siren"
        self._attr_device_info = {
            "identifiers": {("eisenberg", device.device_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if siren is on."""
        state = self.coordinator.siren_states.get(self._device.device_id)
        if state:
            return state.is_on
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn siren on."""
        await self.coordinator.client.set_siren(
            self._device.device_id, on=True
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn siren off."""
        await self.coordinator.client.set_siren(
            self._device.device_id, on=False
        )
```

- [ ] **Step 3: Commit**

```bash
git add custom_components/eisenberg/sensor.py custom_components/eisenberg/switch.py
git commit -m "feat: add battery/signal sensors and siren switch"
```

---

## Task 12: Scripts and Final Wiring

**Files:**
- Create: `scripts/check.sh`
- Verify all imports and wiring

- [ ] **Step 1: Write check script**

```bash
#!/usr/bin/env bash
# scripts/check.sh — run all checks
set -euo pipefail

echo "=== pyright ==="
pyright

echo ""
echo "=== pytest ==="
pytest tests/ -x -q

echo ""
echo "=== ruff check ==="
ruff check eisenberg/ custom_components/ tests/

echo ""
echo "=== ruff format check ==="
ruff format --check eisenberg/ custom_components/ tests/

echo ""
echo "All checks passed."
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/check.sh
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add scripts/check.sh
git commit -m "feat: add check script and finalize project wiring"
```

- [ ] **Step 5: Push**

```bash
git push origin master
```
