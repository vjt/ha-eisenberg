"""Regression tests for base-station connectivity resolution.

Guards issue #14: every `*_base_station_connectivity` sensor read `unknown`
on accounts with real (separate) base stations. The coordinator stores the
link state keyed by the heartbeat `from` field (the base station's deviceId =
the camera's parentId), but the sensor looked it up by the camera's own
deviceId. Those only coincide for base-less cameras (e.g. Essential XL),
which is why it slipped through single-camera testing.

The fix: parse `parentId` into DeviceInfo and resolve connectivity by the
parent base station, falling back to the device's own id when there is no
parent (base-less cameras).
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.eisenberg.binary_sensor import BasestationConnectivity
from eisenberg import DeviceInfo

CAMERA = "AGS_CAMERA_A"
BASE = "BASE_STATION_1"


def _device(device_id: str, parent_id: str | None) -> DeviceInfo:
    raw: dict[str, object] = {
        "deviceId": device_id,
        "deviceName": f"Camera {device_id}",
        "modelId": "VMC2052A",
        "xCloudId": "CLOUD123",
    }
    if parent_id is not None:
        raw["parentId"] = parent_id
    return DeviceInfo.model_validate(raw)


def _make_sensor(device: DeviceInfo) -> tuple[BasestationConnectivity, SimpleNamespace]:
    coordinator = SimpleNamespace(basestation_connection={})
    sensor = BasestationConnectivity.__new__(BasestationConnectivity)
    sensor.coordinator = coordinator  # type: ignore[attr-defined]
    sensor._device = device
    sensor._attr_is_on = None
    sensor.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    return sensor, coordinator


class TestDeviceInfoParentId:
    def test_parent_id_parsed(self) -> None:
        assert _device(CAMERA, BASE).parent_id == BASE

    def test_parent_id_absent_is_none(self) -> None:
        assert _device(CAMERA, None).parent_id is None


class TestBasestationConnectivity:
    def test_resolves_via_parent_base(self) -> None:
        """The core of #14: a camera behind a real base station resolves its
        connectivity from the base station's heartbeat, not its own id."""
        sensor, coord = _make_sensor(_device(CAMERA, BASE))
        coord.basestation_connection[BASE] = "available"

        sensor._handle_coordinator_update()

        assert sensor._attr_is_on is True

    def test_camera_id_alone_does_not_resolve(self) -> None:
        """State stored under the camera's own id must NOT satisfy a camera
        that has a distinct parent base — that was exactly the old bug."""
        sensor, coord = _make_sensor(_device(CAMERA, BASE))
        coord.basestation_connection[CAMERA] = "available"

        sensor._handle_coordinator_update()

        assert sensor._attr_is_on is None

    def test_base_less_camera_falls_back_to_own_id(self) -> None:
        """Base-less cameras (no parentId) publish basestation/is with
        from == their own deviceId; the fallback must still resolve them."""
        sensor, coord = _make_sensor(_device(CAMERA, None))
        coord.basestation_connection[CAMERA] = "unavailable"

        sensor._handle_coordinator_update()

        assert sensor._attr_is_on is False
