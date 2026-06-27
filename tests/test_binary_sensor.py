"""Regression tests for DetectionSensor event de-duplication.

Guards issue #11: AI detection sensors (person/vehicle/animal) cross-fired
across cameras. Home Assistant broadcasts a coordinator update to every
entity on any change, and coordinator.motion_events keeps the last event
per device forever. The old code only checked `not self._attr_is_on`, so
once a sensor auto-reset to off, the next unrelated broadcast found the
device's stale event still matching and re-fired it. With many cameras
this became a cross-camera storm.

The fix tracks the last processed event key per sensor and acts once per
real event, regardless of current on/off state.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.eisenberg.binary_sensor import DetectionSensor
from eisenberg import DeviceInfo
from eisenberg.models import MotionEvent

DEVICE_A = "AGS_CAMERA_A"
DEVICE_B = "AGS_CAMERA_B"


def _device(device_id: str) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Camera {device_id}",
            "modelId": "VMC2052A",
            "xCloudId": "CLOUD123",
        }
    )


def _event(
    device_id: str,
    categories: list[str],
    feed_id: str | None,
    utc_created_date: int,
) -> MotionEvent:
    return MotionEvent.model_validate(
        {
            "type": "motion",
            "deviceId": device_id,
            "objCategories": categories,
            "feedId": feed_id,
            "utcCreatedDate": utc_created_date,
        }
    )


class _Sensor:
    """A DetectionSensor with HA side effects (reset task, state write) stubbed.

    We exercise the real `_handle_coordinator_update` state machine without a
    running HA instance. `writes` counts async_write_ha_state calls (i.e. the
    sensor told HA something changed); `resets` counts reset-timer scheduling.
    """

    def __init__(self, device_id: str, category: str) -> None:
        coordinator = SimpleNamespace(motion_events={})
        self.sensor = DetectionSensor.__new__(DetectionSensor)
        # Minimal init: skip CoordinatorEntity machinery we don't drive here.
        self.sensor.coordinator = coordinator  # type: ignore[attr-defined]
        self.sensor._device = _device(device_id)
        self.sensor._category = category
        self.sensor._entry = SimpleNamespace(options={})
        self.sensor._reset_task = None
        self.sensor._last_seen_event_key = None
        self.sensor._attr_is_on = False

        self.writes = 0
        self.resets = 0
        self.sensor.async_write_ha_state = self._write  # type: ignore[method-assign]
        self.sensor._schedule_reset = self._reset  # type: ignore[method-assign]

    def _write(self) -> None:
        self.writes += 1

    def _reset(self) -> None:
        self.resets += 1

    @property
    def coordinator_events(self) -> dict[str, Any]:
        return self.sensor.coordinator.motion_events

    def broadcast(self) -> None:
        self.sensor._handle_coordinator_update()

    def simulate_timeout(self) -> None:
        """Pretend the auto-reset timer fired and turned the sensor off."""
        self.sensor._attr_is_on = False


@pytest.fixture
def person() -> _Sensor:
    return _Sensor(DEVICE_A, "Person")


def test_matching_event_turns_sensor_on(person: _Sensor) -> None:
    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Person"], "feed-1", 1000)
    person.broadcast()

    assert person.sensor._attr_is_on is True
    assert person.resets == 1
    assert person.writes == 1


def test_stale_event_does_not_refire_after_reset(person: _Sensor) -> None:
    """The core of issue #11.

    A single event must fire exactly once even though many later, unrelated
    coordinator broadcasts arrive while the same stale event still sits in
    motion_events and the sensor has already auto-reset to off.
    """
    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Person"], "feed-1", 1000)
    person.broadcast()
    assert person.sensor._attr_is_on is True

    # Auto-reset timer fires.
    person.simulate_timeout()

    # 50 unrelated broadcasts (other cameras updating siren/mode/motion).
    # The stale event is never cleared from the dict.
    for _ in range(50):
        person.broadcast()

    assert person.sensor._attr_is_on is False
    assert person.resets == 1  # never re-scheduled
    assert person.writes == 1  # only the original turn-on wrote state


def test_genuinely_new_event_fires_again(person: _Sensor) -> None:
    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Person"], "feed-1", 1000)
    person.broadcast()
    person.simulate_timeout()

    # A real new detection: different feed id.
    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Person"], "feed-2", 2000)
    person.broadcast()

    assert person.sensor._attr_is_on is True
    assert person.resets == 2
    assert person.writes == 2


def test_new_event_while_still_on_extends_window(person: _Sensor) -> None:
    """A new event arriving before the timeout reschedules the reset."""
    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Person"], "feed-1", 1000)
    person.broadcast()
    assert person.sensor._attr_is_on is True

    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Person"], "feed-2", 2000)
    person.broadcast()

    assert person.sensor._attr_is_on is True
    assert person.resets == 2  # window extended


def test_non_matching_category_ignored(person: _Sensor) -> None:
    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Vehicle"], "feed-1", 1000)
    person.broadcast()

    assert person.sensor._attr_is_on is False
    assert person.resets == 0
    assert person.writes == 0


def test_other_devices_event_never_fires_us(person: _Sensor) -> None:
    """Sensor for camera A must ignore an event stored under camera B."""
    person.coordinator_events[DEVICE_B] = _event(DEVICE_B, ["Person"], "feed-1", 1000)
    person.broadcast()

    assert person.sensor._attr_is_on is False
    assert person.resets == 0
    assert person.writes == 0


def test_event_without_feed_id_uses_timestamp_key(person: _Sensor) -> None:
    """feedId can be None; the timestamp is the de-dup key fallback."""
    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Person"], None, 1000)
    person.broadcast()
    person.simulate_timeout()

    # Same timestamp, still None feed id -> same key -> no re-fire.
    for _ in range(10):
        person.broadcast()
    assert person.sensor._attr_is_on is False
    assert person.writes == 1

    # New timestamp -> new key -> fires.
    person.coordinator_events[DEVICE_A] = _event(DEVICE_A, ["Person"], None, 2000)
    person.broadcast()
    assert person.sensor._attr_is_on is True
    assert person.writes == 2
