"""Tests for eisenberg Pydantic models.

All test payloads are based on real MQTT messages captured from a live
Arlo system. See docs/specs/2026-04-05-eisenberg-design.md for context.
"""

from typing import ClassVar

from eisenberg.models import (
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
    SpotlightState,
    StreamResponse,
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


class TestArloMode:
    def test_known_modes(self) -> None:
        assert ArloMode.ARM_AWAY == "armAway"
        assert ArloMode.ARM_HOME == "armHome"
        assert ArloMode.STANDBY == "standby"
