"""Tests for eisenberg Pydantic models.

All test payloads are based on real MQTT messages captured from a live
Arlo system. See docs/specs/2026-04-05-eisenberg-design.md for context.
"""

from typing import ClassVar

import pytest
from pydantic import ValidationError

from eisenberg.models import (
    ActiveMode,
    ArloMode,
    Connectivity,
    DeviceInfo,
    DeviceState,
    LastImageSnapshotAvailable,
    LocationInfo,
    LocationState,
    MediaUpload,
    ModeChangeEvent,
    MotionEvent,
    SirenState,
    SnapshotAvailable,
    SpotlightState,
    StreamResponse,
    SubscribeOutcome,
    TopicResult,
)


class TestDeviceState:
    def test_motion_detected(self) -> None:
        state = DeviceState.model_validate(
            {
                "motionDetected": True,
            }
        )
        assert state.motion_detected is True
        assert state.activity_state is None

    def test_activity_state(self) -> None:
        state = DeviceState.model_validate(
            {
                "activityState": "alertStreamActive",
                "dateStarted": 1775341447000,
            }
        )
        assert state.activity_state == "alertStreamActive"
        assert state.date_started == 1775341447000

    def test_signal_strength(self) -> None:
        state = DeviceState.model_validate(
            {
                "signalStrength": 2,
            }
        )
        assert state.signal_strength == 2

    def test_battery_level(self) -> None:
        state = DeviceState.model_validate(
            {
                "batteryLevel": 17,
                "chargingState": "Off",
            }
        )
        assert state.battery_level == 17
        assert state.charging_state == "Off"


class TestMotionEvent:
    PAYLOAD: ClassVar[dict[str, object]] = {
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
        event = ModeChangeEvent.model_validate(
            {
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
            }
        )
        assert event.type == "modeChange"
        assert event.active_mode == ArloMode.STANDBY


class TestActiveMode:
    def test_parse(self) -> None:
        mode = ActiveMode.model_validate(
            {
                "properties": {"mode": "armAway"},
                "revision": 1775339550697,
            }
        )
        assert mode.properties.mode == ArloMode.ARM_AWAY
        assert mode.revision == 1775339550697


class TestSirenState:
    def test_siren_on(self) -> None:
        state = SirenState.model_validate(
            {
                "sirenState": "on",
                "sirenTrigger": "manual",
                "duration": 180,
                "volume": 8,
                "pattern": "alarm",
                "sirenTimestamp": 1775340116000,
            }
        )
        assert state.siren_state == "on"
        assert state.is_on is True
        assert state.duration == 180

    def test_siren_off(self) -> None:
        state = SirenState.model_validate(
            {
                "sirenState": "off",
                "duration": 0,
                "sirenTimestamp": 1775340118000,
            }
        )
        assert state.is_on is False


class TestSpotlightState:
    def test_on_with_intensity(self) -> None:
        state = SpotlightState.model_validate({"enabled": True, "intensity": 75})
        assert state.enabled is True
        assert state.intensity == 75

    def test_off(self) -> None:
        state = SpotlightState.model_validate({"enabled": False, "intensity": 50})
        assert state.enabled is False
        assert state.intensity == 50

    def test_no_intensity(self) -> None:
        state = SpotlightState.model_validate({"enabled": True})
        assert state.enabled is True
        assert state.intensity is None


class TestSnapshotAvailable:
    def test_parse(self) -> None:
        snap = SnapshotAvailable.model_validate(
            {
                "presignedFullFrameSnapshotUrl": "https://example.com/snap.jpg",
                "disablePrivacyZones": False,
            }
        )
        assert snap.presigned_url == "https://example.com/snap.jpg"


class TestLastImageSnapshotAvailable:
    """Issue #26 — some models answer a snapshot request on
    `lastImageSnapshotAvailable` with presignedLastImageUrl instead of
    `fullFrameSnapshotAvailable` with presignedFullFrameSnapshotUrl.
    """

    def test_parse(self) -> None:
        snap = LastImageSnapshotAvailable.model_validate(
            {"presignedLastImageUrl": "https://example.com/last.jpg"}
        )
        assert snap.presigned_url == "https://example.com/last.jpg"

    def test_missing_url_rejected(self) -> None:
        # Parse at the boundary — a payload without the URL is not a snapshot.
        with pytest.raises(ValidationError):
            LastImageSnapshotAvailable.model_validate({})


class TestMediaUpload:
    def test_parse(self) -> None:
        upload = MediaUpload.model_validate(
            {
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
            }
        )
        assert upload.device_id == "AGSEXAMPLE001"
        assert upload.presigned_content_url == "https://example.com/video.mp4"
        assert upload.recording_stopped is True


class TestConnectivity:
    def test_parse(self) -> None:
        conn = Connectivity.model_validate(
            {
                "connectivity": [
                    {
                        "type": "wifi",
                        "connected": True,
                        "ssid": "TestNet",
                        "wifiRssi": -67,
                        "signalStrength": 2,
                        "ipAddr": "192.168.1.100",
                        "connectionState": "Connected",
                    }
                ],
            }
        )
        assert len(conn.connectivity) == 1
        wifi = conn.connectivity[0]
        assert wifi.ssid == "TestNet"
        assert wifi.wifi_rssi == -67
        assert wifi.signal_strength == 2


class TestStreamResponse:
    def test_parse(self) -> None:
        resp = StreamResponse.model_validate(
            {
                "url": "rtsp://wowza.arlo.com:443/vzmodulelive/CAM123",
                "sipCallInfo": {"id": "call123"},
                "iceServers": {"data": []},
            }
        )
        assert resp.url == "rtsp://wowza.arlo.com:443/vzmodulelive/CAM123"


class TestDeviceInfo:
    def test_parse(self) -> None:
        info = DeviceInfo.model_validate(
            {
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
            }
        )
        assert info.device_id == "AGSEXAMPLE001"
        assert info.device_name == "Patio"
        assert info.model_id == "VMC2052A"
        assert info.x_cloud_id == "XCLOUD-0000-000-000000000"

    def test_allowed_mqtt_topics_parsed(self) -> None:
        info = DeviceInfo.model_validate(
            {
                "deviceId": "FB1001A-DOOR",
                "deviceName": "Front Door",
                "modelId": "FB1001A",
                "xCloudId": "XCLOUD-DOORBELL",
                "allowedMqttTopics": [
                    "d/XCLOUD-DOORBELL/out/doorbells/FB1001A-DOOR/#",
                    "u/USER123/in/feed/live",
                ],
            }
        )
        assert info.allowed_mqtt_topics == [
            "d/XCLOUD-DOORBELL/out/doorbells/FB1001A-DOOR/#",
            "u/USER123/in/feed/live",
        ]

    def test_allowed_mqtt_topics_default_empty(self) -> None:
        info = DeviceInfo.model_validate(
            {
                "deviceId": "CAM",
                "deviceName": "Cam",
                "modelId": "VMC2052A",
                "xCloudId": "X",
            }
        )
        assert info.allowed_mqtt_topics == []

    def test_device_type_parsed(self) -> None:
        info = DeviceInfo.model_validate(
            {
                "deviceId": "BASE",
                "deviceName": "Arlo Pro Basisstation",
                "modelId": "VMB4000",
                "xCloudId": "X",
                "deviceType": "basestation",
            }
        )
        assert info.device_type == "basestation"

    def test_device_type_defaults_none(self) -> None:
        info = DeviceInfo.model_validate(
            {
                "deviceId": "CAM",
                "deviceName": "Cam",
                "modelId": "VMC2052A",
                "xCloudId": "X",
            }
        )
        assert info.device_type is None


class TestIsBaseStation:
    """A base station is Arlo infrastructure, not a camera — it must not spawn
    a camera/motion/snapshot entity (issue #24: DirkWeber1972's VMB4000 was
    enumerated as a bogus, non-streamable camera → "Failed to start stream for
    <baseId>"). Classification mirrors pyaarlo (__init__.py): only dedicated
    base/bridge/hub deviceTypes are bases; everything else — cameras, doorbells,
    all-in-one arloq units — is camera-capable. A NEGATIVE filter on purpose:
    the safe failure mode is under-filtering (a base slips through, as today),
    never dropping a real camera/doorbell.
    """

    @staticmethod
    def _dev(device_type: str | None) -> DeviceInfo:
        raw: dict[str, object] = {
            "deviceId": "D",
            "deviceName": "n",
            "modelId": "M",
            "xCloudId": "C",
        }
        if device_type is not None:
            raw["deviceType"] = device_type
        return DeviceInfo.model_validate(raw)

    def test_basestation_is_base(self) -> None:
        assert self._dev("basestation").is_base_station is True

    def test_arlobridge_is_base(self) -> None:
        assert self._dev("arlobridge").is_base_station is True

    def test_hub_is_base(self) -> None:
        assert self._dev("hub").is_base_station is True

    def test_camera_is_not_base(self) -> None:
        assert self._dev("camera").is_base_station is False

    def test_doorbell_is_not_base(self) -> None:
        # Doorbells must stay cameras (#10/#19) even if their deviceType is not
        # literally "camera" — pyaarlo keys doorbells off modelId for exactly
        # this reason, so a positive "==camera" allowlist would drop them.
        assert self._dev("doorbell").is_base_station is False

    def test_arloq_all_in_one_is_not_base(self) -> None:
        # arloq/arloqs are their own base AND a camera; pyaarlo lists them as
        # cameras, so they must keep their camera entity.
        assert self._dev("arloq").is_base_station is False

    def test_missing_device_type_is_not_base(self) -> None:
        # Unknown/absent → default to camera. Never drop a real camera because
        # Arlo omitted the field.
        assert self._dev(None).is_base_station is False

    def test_classification_is_case_insensitive(self) -> None:
        assert self._dev("BaseStation").is_base_station is True


class TestArloMode:
    def test_known_modes(self) -> None:
        assert ArloMode.ARM_AWAY == "armAway"
        assert ArloMode.ARM_HOME == "armHome"
        assert ArloMode.STANDBY == "standby"


class TestLocationInfo:
    def test_parses_gateway_device_ids(self) -> None:
        info = LocationInfo.model_validate(
            {
                "locationId": "loc-uuid",
                "locationName": "Home",
                "gatewayDeviceIds": ["BASE-A", "BASE-B"],
            }
        )
        assert info.location_id == "loc-uuid"
        assert info.location_name == "Home"
        assert info.gateway_device_ids == ["BASE-A", "BASE-B"]

    def test_gateway_device_ids_default_empty(self) -> None:
        info = LocationInfo.model_validate({"locationId": "loc-uuid"})
        assert info.gateway_device_ids == []


class TestLocationState:
    def test_defaults(self) -> None:
        state = LocationState(
            location_id="loc-uuid",
            location_name="Home",
            gateway_device_ids=["BASE-A"],
        )
        assert state.location_id == "loc-uuid"
        assert state.location_name == "Home"
        assert state.gateway_device_ids == ["BASE-A"]
        assert state.active_mode is None
        assert state.mode_revision == 1

    def test_from_location_info(self) -> None:
        info = LocationInfo.model_validate(
            {
                "locationId": "loc-uuid",
                "locationName": "Cabin",
                "gatewayDeviceIds": ["BASE-X", "BASE-Y"],
            }
        )
        state = LocationState.from_info(info)
        assert state.location_id == "loc-uuid"
        assert state.location_name == "Cabin"
        assert state.gateway_device_ids == ["BASE-X", "BASE-Y"]
        assert state.active_mode is None
        assert state.mode_revision == 1


class TestSubscribeOutcome:
    def test_counts_and_refused_topics(self) -> None:
        outcome = SubscribeOutcome(
            results=[
                TopicResult(topic="d/A/out/#", code=0, granted=True),
                TopicResult(topic="d/B/out/#", code=1, granted=True),
                TopicResult(topic="d/C/out/#", code=0x80, granted=False),
                TopicResult(topic="u/USER/in/#", code=0x80, granted=False),
            ]
        )
        assert outcome.granted_count == 2
        assert outcome.refused_count == 2
        assert outcome.refused_topics == ["d/C/out/#", "u/USER/in/#"]

    def test_result_for_returns_matching_topic(self) -> None:
        user_topic = TopicResult(topic="u/USER/in/#", code=1, granted=True)
        outcome = SubscribeOutcome(
            results=[
                TopicResult(topic="d/A/out/#", code=0, granted=True),
                user_topic,
            ]
        )
        assert outcome.result_for("u/USER/in/#") == user_topic
        assert outcome.result_for("u/NOPE/in/#") is None

    def test_empty_outcome(self) -> None:
        outcome = SubscribeOutcome(results=[])
        assert outcome.granted_count == 0
        assert outcome.refused_count == 0
        assert outcome.refused_topics == []
