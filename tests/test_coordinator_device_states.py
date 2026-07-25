"""Pulling child device state from old-style base stations.

Issue #27 (Kevinbull888, 4x VMC5040 behind a VMB5000 SmartHub): battery and
base-station connectivity stayed `unknown` forever, with no errors logged.
His debug log showed the subscription to `d/{xCloudId}/out/#` GRANTED and yet
not a single frame arriving on it — only user-topic media events.

Root cause: cameras that hang off a real base station keep their state on the
hub, and the hub only publishes it when asked. pyaarlo asks (base.py
`update_states()` sends `{action: get, resource: devices}` to devices whose
deviceType is basestation/arlobridge, commenting "Most new devices return
their state from the devices URL but we need to query the original base
stations for their child states"); we never did, so we sat waiting for a push
that was never coming. Base-less cameras (the Essential XL test rig) are their
own gateway and report state in the REST devices payload, which is why this
never showed up locally.

The reply comes back over the event stream — not in the notify HTTP body,
which pyaarlo discards — as `{"resource": "devices", "devices": {id: props}}`,
one property block per child. See pyaarlo backend.py `_event_dispatcher`:

    elif resource == 'devices':
        for device_id in response.get('devices', {}):
            props = response['devices'][device_id]
"""

from __future__ import annotations

from typing import Any

from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg import DeviceInfo

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
    """Client boundary: records which devices got asked for their state."""

    def __init__(self, *, fail: bool = False) -> None:
        self.asked: list[str] = []
        self._fail = fail

    async def request_device_states(self, base_id: str) -> None:
        self.asked.append(base_id)
        if self._fail:
            raise RuntimeError("Arlo said no")


def _coordinator_with_devices(devices: list[DeviceInfo]) -> tuple[Any, _RecordingClient]:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord._devices = devices
    client = _RecordingClient()
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


class TestBaseStationStatePull:
    async def test_asks_every_base_station(self) -> None:
        coord, client = _coordinator_with_devices(
            [
                _device(BASE, "basestation", BASE),
                _device(CAM_A, "camera", BASE),
                _device(CAM_B, "camera", BASE),
            ]
        )
        await coord._pull_base_station_states()
        assert client.asked == [BASE]

    async def test_base_less_account_asks_nobody(self) -> None:
        """Regression guard for the rig this fix cannot be tested on: a
        base-less camera is its own gateway and already reports over REST."""
        coord, client = _coordinator_with_devices([_device(CAM_A, "camera", CAM_A)])
        await coord._pull_base_station_states()
        assert client.asked == []

    async def test_a_failing_pull_does_not_break_startup(self) -> None:
        coord, _ = _coordinator_with_devices([_device(BASE, "basestation", BASE)])
        coord.client = _RecordingClient(fail=True)  # type: ignore[assignment]
        await coord._pull_base_station_states()  # must not raise
