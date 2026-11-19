"""Doorbell button presses.

A video doorbell reports a press as `{"buttonPressed": true}` in its state
frame and never reports the release. Before this, the frame was logged and
dropped — `Camera <id> state: motion=None activity=None
raw={"buttonPressed": true}` — so a doorbell had the same entities as any
camera and no way to tell that someone had rung.

The coordinator now counts presses per device; the "Doorbell pressed" binary
sensor and the doorbell event entity each act once per new count.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from custom_components.eisenberg.binary_sensor import DoorbellPressSensor
from custom_components.eisenberg.const import DOORBELL_EVENT_RING
from custom_components.eisenberg.coordinator import EisenbergCoordinator
from custom_components.eisenberg.event import DoorbellEvent
from eisenberg import DeviceInfo

BELL = "AGG141B70148D"
CAM = "5GG39A71A13BD"
CLOUD = "A92ZBCN6-2400-115-112185863"
BELL_TOPIC = f"d/{CLOUD}/out/doorbells/{BELL}/is"


def _device(device_id: str, device_type: str) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Device {device_id}",
            "modelId": "AVD4001" if device_type == "doorbell" else "VMC5040",
            "deviceType": device_type,
            "xCloudId": CLOUD,
        }
    )


def _coordinator() -> Any:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord.device_states = {}
    coord.spotlight_states = {}
    coord.doorbell_presses = {}
    coord.data = {}
    coord._push_to_entities = lambda: None  # type: ignore[method-assign]
    return coord


class TestCoordinatorCountsPresses:
    async def test_press_is_counted(self) -> None:
        coord = _coordinator()
        await coord._handle_camera_state(BELL_TOPIC, {"properties": {"buttonPressed": True}})
        assert coord.doorbell_presses[BELL] == 1

    async def test_every_press_counts(self) -> None:
        coord = _coordinator()
        for _ in range(3):
            await coord._handle_camera_state(BELL_TOPIC, {"properties": {"buttonPressed": True}})
        assert coord.doorbell_presses[BELL] == 3

    async def test_frames_without_a_press_do_not_count(self) -> None:
        coord = _coordinator()
        await coord._handle_camera_state(BELL_TOPIC, {"properties": {"motionDetected": True}})
        await coord._handle_camera_state(BELL_TOPIC, {"properties": {"buttonPressed": False}})
        assert BELL not in coord.doorbell_presses

    async def test_press_frame_still_updates_device_state(self) -> None:
        coord = _coordinator()
        await coord._handle_camera_state(
            BELL_TOPIC, {"properties": {"buttonPressed": True, "batteryLevel": 80}}
        )
        assert coord.device_states[BELL].battery_level == 80
        assert coord.doorbell_presses[BELL] == 1

    def test_only_doorbells_get_press_entities(self) -> None:
        coord = _coordinator()
        coord._devices = [_device(BELL, "doorbell"), _device(CAM, "camera")]
        assert [d.device_id for d in coord.doorbells] == [BELL]


def _stub_entity(entity: Any, presses: dict[str, int]) -> list[str]:
    """Wire an entity to a fake coordinator; returns the list of state writes."""
    writes: list[str] = []
    entity.coordinator = SimpleNamespace(doorbell_presses=presses)
    entity._device = _device(BELL, "doorbell")
    entity._last_seen_presses = presses.get(BELL, 0)
    entity.async_write_ha_state = lambda: writes.append("write")
    return writes


class TestDoorbellPressSensor:
    def _sensor(self, presses: dict[str, int]) -> tuple[Any, list[str], list[str]]:
        sensor = DoorbellPressSensor.__new__(DoorbellPressSensor)
        writes = _stub_entity(sensor, presses)
        resets: list[str] = []
        sensor._reset_task = None
        sensor._attr_is_on = False
        sensor._schedule_reset = lambda: resets.append("reset")
        return sensor, writes, resets

    def test_new_press_turns_it_on(self) -> None:
        presses: dict[str, int] = {}
        sensor, writes, resets = self._sensor(presses)
        presses[BELL] = 1
        sensor._handle_coordinator_update()
        assert sensor._attr_is_on is True
        assert len(writes) == 1
        assert len(resets) == 1

    def test_unrelated_broadcasts_do_not_refire(self) -> None:
        presses: dict[str, int] = {}
        sensor, writes, resets = self._sensor(presses)
        presses[BELL] = 1
        sensor._handle_coordinator_update()
        sensor._attr_is_on = False  # reset timer fired
        for _ in range(20):
            sensor._handle_coordinator_update()
        assert sensor._attr_is_on is False
        assert len(writes) == 1
        assert len(resets) == 1

    def test_press_before_entity_existed_is_ignored(self) -> None:
        sensor, writes, _ = self._sensor({BELL: 4})
        sensor._handle_coordinator_update()
        assert sensor._attr_is_on is False
        assert writes == []

    def test_second_press_fires_again(self) -> None:
        presses: dict[str, int] = {}
        sensor, _, resets = self._sensor(presses)
        presses[BELL] = 1
        sensor._handle_coordinator_update()
        presses[BELL] = 2
        sensor._handle_coordinator_update()
        assert len(resets) == 2


class TestDoorbellEvent:
    def _event(self, presses: dict[str, int]) -> tuple[Any, list[str], list[str]]:
        entity = DoorbellEvent.__new__(DoorbellEvent)
        writes = _stub_entity(entity, presses)
        fired: list[str] = []
        entity._trigger_event = lambda event_type, event_attributes=None: fired.append(  # pyright: ignore[reportUnknownLambdaType]
            event_type
        )
        return entity, writes, fired

    def test_press_fires_ring(self) -> None:
        presses: dict[str, int] = {}
        entity, writes, fired = self._event(presses)
        presses[BELL] = 1
        entity._handle_coordinator_update()
        assert fired == [DOORBELL_EVENT_RING]
        assert len(writes) == 1

    def test_one_ring_per_press(self) -> None:
        presses: dict[str, int] = {}
        entity, _, fired = self._event(presses)
        presses[BELL] = 1
        for _ in range(10):
            entity._handle_coordinator_update()
        presses[BELL] = 2
        entity._handle_coordinator_update()
        assert fired == [DOORBELL_EVENT_RING, DOORBELL_EVENT_RING]

    def test_press_before_entity_existed_is_ignored(self) -> None:
        entity, writes, fired = self._event({BELL: 2})
        entity._handle_coordinator_update()
        assert fired == []
        assert writes == []
