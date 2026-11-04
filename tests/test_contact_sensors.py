"""Arlo MS1001 all-in-one sensors as native contact sensors.

An MS1001 is listed with deviceType "sensors" and publishes its state as
resource `sensors/<id>`, e.g. `{"contactState": true}` when the door opens
(pyaarlo's ArloSensor.is_open reads the same key). A field capture showed the
broker leaving that resource uncovered, with the frame arriving over SSE.
It is not a camera: it must get a contact sensor and a battery, never a stream,
motion or AI detection entities.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.eisenberg.binary_sensor import (
    ContactSensor,
    MotionSensor,
    build_binary_sensors,
)
from custom_components.eisenberg.coordinator import EisenbergCoordinator
from custom_components.eisenberg.sensor import BatterySensor, build_sensors
from eisenberg import DeviceInfo
from eisenberg.models import DeviceState, SubscribeOutcome, TopicResult
from eisenberg.mqtt import topic_matches

HUB_CLOUD = "HUBXC"
SENSOR_ID = "MS1"
CAM_ID = "CAM1"


def _sensor(properties: dict[str, Any] | None = None) -> DeviceInfo:
    raw: dict[str, Any] = {
        "deviceId": SENSOR_ID,
        "deviceName": "Front Door",
        "modelId": "MS1001",
        "deviceType": "sensors",
        "xCloudId": HUB_CLOUD,
        "parentId": "HUB1",
    }
    if properties is not None:
        raw["properties"] = properties
    return DeviceInfo.model_validate(raw)


def _camera() -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": CAM_ID,
            "deviceName": "Patio",
            "modelId": "VMC4030P",
            "deviceType": "camera",
            "xCloudId": HUB_CLOUD,
            "parentId": "HUB1",
        }
    )


def _coord(devices: list[DeviceInfo]) -> EisenbergCoordinator:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord._devices = devices
    coord.device_states = {}
    coord.spotlight_states = {}
    coord.data = {}
    coord._push_to_entities = lambda: None  # type: ignore[method-assign]
    return coord


def _contact(coord: Any, device: DeviceInfo) -> ContactSensor:
    sensor = ContactSensor(coord, device)
    sensor.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    return sensor


class TestClassification:
    def test_sensor_is_not_a_camera(self) -> None:
        coord = _coord([_sensor(), _camera()])
        assert [d.device_id for d in coord.cameras] == [CAM_ID]
        assert [d.device_id for d in coord.contact_sensors] == [SENSOR_ID]


class TestStateDelivery:
    def test_uncovered_sensor_resource_goes_to_sse(self) -> None:
        coord = _coord([_sensor()])
        outcome = SubscribeOutcome(
            results=[TopicResult(topic=f"d/{HUB_CLOUD}/out/#", code=0x80, granted=False)]
        )
        assert coord._sse_gap_resources(outcome) == {f"sensors/{SENSOR_ID}": HUB_CLOUD}

    async def test_sse_contact_frames_reach_device_state(self) -> None:
        coord = _coord([_sensor()])
        coord._set_sse_resources({f"sensors/{SENSOR_ID}": HUB_CLOUD})
        frame = {
            "from": SENSOR_ID,
            "action": "is",
            "resource": f"sensors/{SENSOR_ID}",
            "properties": {"contactState": True},
        }
        await coord._handle_sse_event(frame)
        assert coord.device_states[SENSOR_ID].contact_state is True

        frame["properties"] = {"contactState": False}
        await coord._handle_sse_event(frame)
        assert coord.device_states[SENSOR_ID].contact_state is False

    def test_mqtt_sensor_topic_has_a_handler(self) -> None:
        coord = _coord([_sensor()])
        topic = f"d/{HUB_CLOUD}/out/sensors/{SENSOR_ID}/is"
        assert any(topic_matches(pattern, topic) for pattern, _ in coord._event_routes())


class TestEntities:
    def _entry(self) -> Any:
        return SimpleNamespace(options={})

    def test_sensor_gets_contact_and_battery_only(self) -> None:
        coord = _coord([_sensor()])
        binaries = build_binary_sensors(coord, self._entry())
        assert [type(e) for e in binaries] == [ContactSensor]
        assert [type(e) for e in build_sensors(coord)] == [BatterySensor]

    def test_camera_gets_no_contact_sensor(self) -> None:
        coord = _coord([_camera()])
        binaries = build_binary_sensors(coord, self._entry())
        assert ContactSensor not in {type(e) for e in binaries}
        assert MotionSensor in {type(e) for e in binaries}

    def test_contact_is_an_opening_sensor(self) -> None:
        sensor = _contact(_coord([_sensor()]), _sensor())
        assert sensor.device_class == BinarySensorDeviceClass.OPENING
        assert sensor.unique_id == f"{SENSOR_ID}_contact"

    def test_contact_unknown_until_reported(self) -> None:
        coord = _coord([_sensor()])
        sensor = _contact(coord, _sensor())
        sensor._handle_coordinator_update()
        assert sensor.is_on is None

    def test_contact_seeded_from_device_list(self) -> None:
        coord = _coord([_sensor({"contactState": True})])
        sensor = _contact(coord, _sensor({"contactState": True}))
        assert sensor.is_on is True

    def test_contact_follows_reported_state(self) -> None:
        coord = _coord([_sensor()])
        sensor = _contact(coord, _sensor())
        coord.device_states[SENSOR_ID] = DeviceState.model_validate({"contactState": True})
        sensor._handle_coordinator_update()
        assert sensor.is_on is True
        coord.device_states[SENSOR_ID] = DeviceState.model_validate({"contactState": False})
        sensor._handle_coordinator_update()
        assert sensor.is_on is False

    def test_contact_registers_the_device(self) -> None:
        sensor = _contact(_coord([_sensor()]), _sensor())
        info = sensor.device_info
        assert info is not None
        assert info.get("model") == "MS1001"
        assert info.get("name") == "Front Door"
