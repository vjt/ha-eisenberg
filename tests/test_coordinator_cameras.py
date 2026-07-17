"""coordinator.cameras — the device list the per-device entity platforms
(camera, motion/detection sensors, snapshot button, spotlight, siren, battery)
iterate. It must exclude base stations so a base never becomes a bogus,
non-streamable camera (issue #24, DirkWeber1972's VMB4000 → "Failed to start
stream for <baseId>"). Bases must still stay in `.devices`, which mode/location/
connectivity resolution and MQTT subscription depend on (a base is a gateway).
"""

from __future__ import annotations

from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg import DeviceInfo


def _dev(device_id: str, device_type: str | None) -> DeviceInfo:
    raw: dict[str, object] = {
        "deviceId": device_id,
        "deviceName": f"dev {device_id}",
        "modelId": "M",
        "xCloudId": "C",
    }
    if device_type is not None:
        raw["deviceType"] = device_type
    return DeviceInfo.model_validate(raw)


def _coord(devices: list[DeviceInfo]) -> EisenbergCoordinator:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord._devices = devices
    return coord


class TestCoordinatorCameras:
    def test_excludes_base_station(self) -> None:
        base = _dev("BASE", "basestation")
        cam = _dev("CAM", "camera")
        coord = _coord([base, cam])
        assert [d.device_id for d in coord.cameras] == ["CAM"]

    def test_devices_still_includes_base(self) -> None:
        base = _dev("BASE", "basestation")
        cam = _dev("CAM", "camera")
        coord = _coord([base, cam])
        # Bases remain in .devices — mode/location/connectivity and MQTT
        # subscription resolve gateways from the full set.
        assert [d.device_id for d in coord.devices] == ["BASE", "CAM"]

    def test_keeps_doorbell_and_unknown_types(self) -> None:
        doorbell = _dev("DOOR", "doorbell")
        unknown = _dev("MYST", None)
        coord = _coord([doorbell, unknown])
        assert [d.device_id for d in coord.cameras] == ["DOOR", "MYST"]

    def test_baseless_account_unchanged(self) -> None:
        # No base-type device (vjt's Essential XL account) → cameras == devices.
        cams = [_dev("A", "camera"), _dev("B", "camera")]
        coord = _coord(cams)
        assert coord.cameras == coord.devices
