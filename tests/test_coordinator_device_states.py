"""Pulling child device state from old-style base stations.

Issue #27 (Kevinbull888, 4x VMC5040 behind a VMB5000 SmartHub): battery and
base-station connectivity stayed `unknown` forever, with no errors logged.
His debug log showed the subscription to `d/{xCloudId}/out/#` GRANTED and yet
not a single frame arriving on it — only user-topic media events.

There are two halves to it, and shipping only the second one proved that in
the field — the pull went out, Arlo accepted it, and still nothing came back.

1. **Register with the hub.** A base station publishes nothing to a session
   that has not registered with it. Being granted `d/{xCloudId}/out/#` is a
   subscription with Arlo's *broker*, not with the *hub*; until the hub is
   told to publish to `{userId}_web` it stays silent, so every question goes
   unanswered. pyaarlo does this in base.py `_ping_and_check_reply()`, and
   repeats it on refresh because the registration expires.
2. **Ask for the state.** Children's battery, signal and connectionState live
   on the hub and are only handed over on request — pyaarlo's base.py
   `update_states()` sends `{action: get, resource: devices}`, commenting
   "Most new devices return their state from the devices URL but we need to
   query the original base stations for their child states".

Base-less cameras (the Essential XL test rig) are their own gateway, are
published to directly and report state in the REST devices payload, which is
why none of this ever showed up locally.

The reply comes back over the event stream — not in the notify HTTP body,
which pyaarlo discards — as `{"resource": "devices", "devices": {id: props}}`,
one property block per child. See pyaarlo backend.py `_event_dispatcher`:

    elif resource == 'devices':
        for device_id in response.get('devices', {}):
            props = response['devices'][device_id]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg import DeviceInfo

if TYPE_CHECKING:
    import pytest

BASE = "5GP59A7RA0411"
CAM_A = "5GG39A71A13BD"
CAM_B = "5GG39A78A0FEE"
CLOUD = "A92ZBCN6-2400-115-112185863"
DEVICES_TOPIC = f"d/{CLOUD}/out/devices/is"


def _device(device_id: str, device_type: str, parent_id: str) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Device {device_id}",
            "modelId": "VMC5040" if device_type == "camera" else "VMB5000",
            "deviceType": device_type,
            "xCloudId": CLOUD,
            "parentId": parent_id,
        }
    )


def _payload(devices: dict[str, Any]) -> dict[str, Any]:
    return {"resource": "devices", "from": BASE, "devices": devices}


class _RecordingCoordinator:
    """Coordinator with only the HA push boundary stubbed out."""

    def __init__(self) -> None:
        self.coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
        self.coord.device_states = {}
        self.coord.basestation_connection = {}
        self.coord.data = {}
        self.pushes = 0

        def _push(data: Any) -> None:
            self.pushes += 1

        self.coord.async_set_updated_data = _push  # type: ignore[method-assign]


class _RecordingClient:
    """Client boundary: records what we asked each base, in order."""

    def __init__(self, *, fail_register: bool = False, fail_pull: bool = False) -> None:
        self.calls: list[str] = []
        self._fail_register = fail_register
        self._fail_pull = fail_pull

    async def register_event_subscription(self, base_id: str) -> None:
        self.calls.append(f"register:{base_id}")
        if self._fail_register:
            raise RuntimeError("Arlo said no")

    async def request_device_states(self, base_id: str) -> None:
        self.calls.append(f"pull:{base_id}")
        if self._fail_pull:
            raise RuntimeError("Arlo said no")


def _coordinator_with_devices(
    devices: list[DeviceInfo], client: _RecordingClient | None = None
) -> tuple[Any, _RecordingClient]:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord._devices = devices
    coord.device_states = {}
    coord.basestation_connection = {}
    coord.data = {}
    coord.async_set_updated_data = lambda data: None  # type: ignore[method-assign]
    client = client or _RecordingClient()
    coord.client = client  # type: ignore[assignment]
    return coord, client


class TestDevicesResponseHandler:
    async def test_battery_lands_for_each_child(self) -> None:
        """The whole point of #27: battery stops being unknown."""
        rec = _RecordingCoordinator()
        await rec.coord._handle_devices_response(
            DEVICES_TOPIC,
            _payload(
                {
                    CAM_A: {"batteryLevel": 87},
                    CAM_B: {"batteryLevel": 42},
                }
            ),
        )
        assert rec.coord.device_states[CAM_A].battery_level == 87
        assert rec.coord.device_states[CAM_B].battery_level == 42

    async def test_signal_strength_lands(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_devices_response(
            DEVICES_TOPIC, _payload({CAM_A: {"signalStrength": 3}})
        )
        assert rec.coord.device_states[CAM_A].signal_strength == 3

    async def test_connection_state_keyed_by_the_device_it_describes(self) -> None:
        """The base's own entry is what the connectivity sensor resolves
        against: a camera looks up `basestation_connection[parentId]`."""
        rec = _RecordingCoordinator()
        await rec.coord._handle_devices_response(
            DEVICES_TOPIC, _payload({BASE: {"connectionState": "available"}})
        )
        assert rec.coord.basestation_connection[BASE] == "available"

    async def test_pushes_one_update_so_entities_refresh(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_devices_response(
            DEVICES_TOPIC,
            _payload({CAM_A: {"batteryLevel": 87}, CAM_B: {"batteryLevel": 42}}),
        )
        assert rec.pushes == 1

    async def test_ignores_responses_for_other_resources(self) -> None:
        """Registered on the devices/ subtree, so it also sees per-device
        `states/is` frames that a different handler owns."""
        rec = _RecordingCoordinator()
        await rec.coord._handle_devices_response(
            f"d/{CLOUD}/out/devices/{CAM_A}/states/is",
            {"resource": f"devices/{CAM_A}", "states": {"activeMode": "armHome"}},
        )
        assert rec.coord.device_states == {}
        assert rec.pushes == 0

    async def test_malformed_child_does_not_poison_the_others(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_devices_response(
            DEVICES_TOPIC,
            _payload({CAM_A: "not-a-property-block", CAM_B: {"batteryLevel": 42}}),
        )
        assert rec.coord.device_states[CAM_B].battery_level == 42
        assert CAM_A not in rec.coord.device_states

    async def test_empty_devices_block_pushes_nothing(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_devices_response(DEVICES_TOPIC, _payload({}))
        assert rec.pushes == 0


class TestBaseStationBringUp:
    async def test_registers_before_asking_for_state(self) -> None:
        """A base station publishes nothing to a session that never
        registered, so the pull is useless until the subscription exists.
        Order is the fix, not either call alone."""
        coord, client = _coordinator_with_devices(
            [_device(BASE, "basestation", BASE), _device(CAM_A, "camera", BASE)]
        )
        await coord._bring_up_base_stations()
        assert client.calls == [f"register:{BASE}", f"pull:{BASE}"]

    async def test_base_less_account_asks_nobody(self) -> None:
        """Regression guard for the rig this cannot be tested on: a
        base-less camera is its own gateway and already reports over REST."""
        coord, client = _coordinator_with_devices([_device(CAM_A, "camera", CAM_A)])
        await coord._bring_up_base_stations()
        assert client.calls == []

    async def test_successful_registration_marks_the_base_available(self) -> None:
        """The registration's own outcome is a connectivity signal: it is
        a round trip to the hub, so a reply means the hub is reachable."""
        coord, _ = _coordinator_with_devices([_device(BASE, "basestation", BASE)])
        await coord._bring_up_base_stations()
        assert coord.basestation_connection[BASE] == "available"

    async def test_failed_registration_marks_the_base_unavailable(self) -> None:
        coord, _ = _coordinator_with_devices(
            [_device(BASE, "basestation", BASE)], _RecordingClient(fail_register=True)
        )
        await coord._bring_up_base_stations()
        assert coord.basestation_connection[BASE] == "unavailable"

    async def test_failed_registration_skips_the_pointless_pull(self) -> None:
        coord, client = _coordinator_with_devices(
            [_device(BASE, "basestation", BASE)], _RecordingClient(fail_register=True)
        )
        await coord._bring_up_base_stations()
        assert client.calls == [f"register:{BASE}"]

    async def test_a_failing_pull_does_not_break_startup(self) -> None:
        coord, _ = _coordinator_with_devices(
            [_device(BASE, "basestation", BASE)], _RecordingClient(fail_pull=True)
        )
        await coord._bring_up_base_stations()  # must not raise


class _FullClient(_RecordingClient):
    """Adds the REST + snapshot surface the later fixes touch."""

    def __init__(self, devices: list[DeviceInfo] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self._rest_devices = devices or []
        self.snapshots: list[str] = []
        self.fail_get_devices = False

    async def get_devices(self) -> list[DeviceInfo]:
        self.calls.append("get_devices")
        if self.fail_get_devices:
            raise RuntimeError("Arlo said no")
        return self._rest_devices

    async def request_snapshot(self, device_id: str) -> None:
        self.snapshots.append(device_id)


def _device_with_props(device_id: str, props: dict[str, Any]) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Device {device_id}",
            "modelId": "VMC5040",
            "deviceType": "camera",
            "xCloudId": CLOUD,
            "parentId": BASE,
            "properties": props,
        }
    )


class TestRestDevicePropertiesRefresh:
    """Old-style base-stationed cameras report battery in the REST devices
    list, not over MQTT — and that list is only meaningful if we read it
    more than once. pyaarlo re-reads it periodically (`_refresh_devices`)."""

    async def test_battery_arrives_from_the_rest_payload(self) -> None:
        coord, _ = _coordinator_with_devices(
            [_device(CAM_A, "camera", BASE)],
            _FullClient([_device_with_props(CAM_A, {"batteryLevel": 64})]),
        )
        await coord._refresh_device_properties()
        assert coord.device_states[CAM_A].battery_level == 64

    async def test_does_not_clobber_live_mqtt_state(self) -> None:
        """A REST block carrying only battery must not wipe the activity
        state MQTT just delivered."""
        client = _FullClient([_device_with_props(CAM_A, {"batteryLevel": 64})])
        coord, _ = _coordinator_with_devices([_device(CAM_A, "camera", BASE)], client)
        await coord._handle_devices_response(
            DEVICES_TOPIC, _payload({CAM_A: {"activityState": "userStreamActive"}})
        )
        await coord._refresh_device_properties()
        assert coord.device_states[CAM_A].battery_level == 64
        assert coord.device_states[CAM_A].activity_state == "userStreamActive"

    async def test_a_failing_refresh_does_not_raise(self) -> None:
        client = _FullClient([])
        client.fail_get_devices = True
        coord, _ = _coordinator_with_devices([_device(CAM_A, "camera", BASE)], client)
        await coord._refresh_device_properties()  # must not raise

    async def test_device_without_properties_is_skipped(self) -> None:
        coord, _ = _coordinator_with_devices(
            [_device(CAM_A, "camera", BASE)], _FullClient([_device(CAM_A, "camera", BASE)])
        )
        await coord._refresh_device_properties()
        assert CAM_A not in coord.device_states


class TestInitialSnapshots:
    async def test_never_asks_a_base_station_for_a_snapshot(self) -> None:
        """A base station is not a camera: Arlo answers error 4000
        'Resource not found', which showed up as noise in the field."""
        client = _FullClient()
        coord, _ = _coordinator_with_devices(
            [_device(BASE, "basestation", BASE), _device(CAM_A, "camera", BASE)], client
        )
        coord.mode_for_device = lambda device_id: "armHome"  # type: ignore[method-assign]
        await coord._request_initial_snapshots()
        assert client.snapshots == [CAM_A]


class TestSubscriptionAck:
    async def test_ack_confirms_the_base_is_reachable(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_subscription_ack(
            f"d/{CLOUD}/out/subscriptions/USER_web/is",
            {
                "from": BASE,
                "to": "USER_web",
                "action": "is",
                "resource": "subscriptions/USER_web",
                "properties": {"devices": [BASE]},
            },
        )
        assert rec.coord.basestation_connection[BASE] == "available"


class TestRenewalGuard:
    async def test_health_tick_right_after_startup_does_not_re_register(self) -> None:
        """Startup already registered; the first health refresh landing a
        second later must not do it all again (it did, in the field)."""
        coord, client = _coordinator_with_devices([_device(BASE, "basestation", BASE)])
        await coord._bring_up_base_stations()
        client.calls.clear()
        await coord._maybe_renew_base_stations()
        assert client.calls == []

    async def test_renews_once_the_interval_has_passed(self) -> None:
        coord, client = _coordinator_with_devices([_device(BASE, "basestation", BASE)])
        await coord._bring_up_base_stations()
        client.calls.clear()
        coord._last_base_bringup -= 10_000  # far enough in the past
        await coord._maybe_renew_base_stations()
        assert client.calls == [f"register:{BASE}", f"pull:{BASE}"]


class TestConnectivityFromDeviceProperties:
    """The REST device list carries `connectionState` for the device it
    describes, but the connectivity sensor reads a different map — so the
    value arrived, was parsed, and was filed where nothing looks for it.
    On a base-less camera that left the sensor `unknown` forever, with the
    answer sitting in the payload the whole time."""

    async def test_connection_state_reaches_the_map_the_sensor_reads(self) -> None:
        coord, _ = _coordinator_with_devices(
            [_device(CAM_A, "camera", CAM_A)],
            _FullClient([_device_with_props(CAM_A, {"connectionState": "available"})]),
        )
        await coord._refresh_device_properties()
        assert coord.basestation_connection[CAM_A] == "available"

    async def test_properties_without_connection_state_leave_it_alone(self) -> None:
        coord, _ = _coordinator_with_devices(
            [_device(CAM_A, "camera", CAM_A)],
            _FullClient([_device_with_props(CAM_A, {"batteryLevel": 50})]),
        )
        coord.basestation_connection[CAM_A] = "available"
        await coord._refresh_device_properties()
        assert coord.basestation_connection[CAM_A] == "available"

    async def test_a_base_reporting_itself_resolves_its_cameras(self) -> None:
        """A camera resolves connectivity through its parent, so the base's
        own entry is the one that matters on base-stationed accounts."""
        coord, _ = _coordinator_with_devices(
            [_device(BASE, "basestation", BASE), _device(CAM_A, "camera", BASE)],
            _FullClient([_device_with_props(BASE, {"connectionState": "available"})]),
        )
        await coord._refresh_device_properties()
        assert coord.basestation_connection[BASE] == "available"


class TestCameraStateDoesNotClobberHubState:
    """Issue #24 (DirkWeber1972, 11 cameras behind a VMB4000).

    His 0.3.17 log has the whole #27 chain working — registration
    acknowledged, `devices/is` answered, `Base station reported state for 11
    device(s)` — and battery still `unknown` on 8 of 11 cameras. The reason
    is 200 milliseconds wide: the hub fills battery and signal, then the
    per-camera `cameras/{id}/is` frames land carrying only motion/activity
    and REPLACE the stored state wholesale, before any entity has read it.
    His REST device list returns `properties={}` for every device, so the
    hub reply is the only source there is — and we threw it away.

    `_merge_device_state` was added in 0.3.16 and wired into the two new
    paths; this, the oldest and by far the busiest writer, kept assigning.
    """

    CAMERA_TOPIC = f"d/{CLOUD}/out/cameras/{CAM_A}/is"

    def _coordinator(self) -> Any:
        rec = _RecordingCoordinator()
        rec.coord.spotlight_states = {}
        return rec.coord

    async def test_battery_from_the_hub_survives_a_camera_state_frame(self) -> None:
        coord = self._coordinator()
        await coord._handle_devices_response(
            DEVICES_TOPIC, _payload({CAM_A: {"batteryLevel": 87, "signalStrength": 3}})
        )
        await coord._handle_camera_state(
            self.CAMERA_TOPIC, {"properties": {"activityState": "idle"}}
        )
        assert coord.device_states[CAM_A].battery_level == 87
        assert coord.device_states[CAM_A].signal_strength == 3

    async def test_camera_frame_still_applies_its_own_fields(self) -> None:
        coord = self._coordinator()
        await coord._handle_devices_response(
            DEVICES_TOPIC, _payload({CAM_A: {"batteryLevel": 87}})
        )
        await coord._handle_camera_state(
            self.CAMERA_TOPIC, {"properties": {"motionDetected": True}}
        )
        assert coord.device_states[CAM_A].motion_detected is True

    async def test_a_fresher_battery_reading_wins(self) -> None:
        """Merging must not mean the first value sticks forever."""
        coord = self._coordinator()
        await coord._handle_devices_response(
            DEVICES_TOPIC, _payload({CAM_A: {"batteryLevel": 87}})
        )
        await coord._handle_camera_state(self.CAMERA_TOPIC, {"properties": {"batteryLevel": 42}})
        assert coord.device_states[CAM_A].battery_level == 42

    async def test_raw_state_block_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The log that found this could not show what those frames carried:
        we logged the two parsed fields and dropped the block. A field report
        should never again be one unloggable payload away from an answer."""
        coord = self._coordinator()
        with caplog.at_level(logging.DEBUG):
            await coord._handle_camera_state(
                self.CAMERA_TOPIC, {"properties": {"batteryLevel": 42, "chargingState": "Off"}}
            )
        assert '"chargingState": "Off"' in caplog.text
