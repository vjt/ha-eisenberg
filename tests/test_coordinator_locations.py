"""Device->location resolution for per-location mode tracking (#16).

A device belongs to a location when its gateway — its base station
(`parentId`), or its own `deviceId` when base-less — is in that location's
`gatewayDeviceIds`.
"""

from __future__ import annotations

from custom_components.eisenberg.coordinator import resolve_location_for_device
from eisenberg import DeviceInfo
from eisenberg.models import LocationState


def _device(device_id: str, parent_id: str | None = None) -> DeviceInfo:
    raw: dict[str, object] = {
        "deviceId": device_id,
        "deviceName": f"Camera {device_id}",
        "modelId": "VMC2052A",
        "xCloudId": "CLOUD",
    }
    if parent_id is not None:
        raw["parentId"] = parent_id
    return DeviceInfo.model_validate(raw)


LOC_A = LocationState(
    location_id="locA", location_name="Home", gateway_device_ids=["BASE-1", "BASE-2"]
)
LOC_B = LocationState(location_id="locB", location_name="Cabin", gateway_device_ids=["BASE-3"])


class TestResolveLocationForDevice:
    def test_camera_resolves_via_parent_base(self) -> None:
        dev = _device("CAM", parent_id="BASE-3")
        assert resolve_location_for_device(dev, [LOC_A, LOC_B]) is LOC_B

    def test_baseless_camera_resolves_via_own_id(self) -> None:
        # Essential XL is its own base: its deviceId is the gateway.
        dev = _device("BASE-1", parent_id=None)
        assert resolve_location_for_device(dev, [LOC_A, LOC_B]) is LOC_A

    def test_no_match_returns_none(self) -> None:
        dev = _device("CAM", parent_id="BASE-UNKNOWN")
        assert resolve_location_for_device(dev, [LOC_A, LOC_B]) is None

    def test_resolves_via_owner_prefixed_gateway_id(self) -> None:
        # Real Arlo returns gatewayDeviceIds as "{ownerId}_{deviceId}"; a
        # base-less camera's gateway is its bare deviceId. pyaarlo strips the
        # prefix (location.py:226) — we must match the same way.
        loc = LocationState(
            location_id="loc",
            gateway_device_ids=["KRQFS-207-166989294_AGS5537BD0D0D"],
        )
        dev = _device("AGS5537BD0D0D", parent_id=None)
        assert resolve_location_for_device(dev, [loc]) is loc
