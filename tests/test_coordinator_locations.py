"""Device->location resolution for per-location mode tracking (#16).

A device belongs to a location when its gateway — its base station
(`parentId`), or its own `deviceId` when base-less — is in that location's
`gatewayDeviceIds`.
"""

from __future__ import annotations

from custom_components.eisenberg.coordinator import (
    relevant_locations,
    resolve_location_for_device,
)
from eisenberg import DeviceInfo
from eisenberg.models import LocationInfo, LocationState


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


class TestRelevantLocations:
    """Keep only locations that actually gateway a discovered device (#21).

    Shared-device accounts get an empty own default location plus the shared
    location that gateways the base. The empty one would spawn a phantom mode
    select and, worse, absorb mode set/get that never reaches the base. Prune
    it — unless NO location gateways anything (Arlo omitted gatewayDeviceIds),
    in which case keep all so single-location accounts still resolve via the
    fallback.
    """

    def test_drops_empty_own_location_keeps_shared(self) -> None:
        # Dirk's exact shape: own location empty, shared location gateways base.
        own = LocationInfo(locationId="own-empty", gatewayDeviceIds=[])
        shared = LocationInfo(
            locationId="shared-real",
            gatewayDeviceIds=["P7EK4-183-13483128_4RD17372A3A28"],
        )
        # 13 devices all gateway through the shared base 4RD17372A3A28.
        devices = [_device("CAM", parent_id="4RD17372A3A28")]
        result = relevant_locations([own, shared], devices)
        assert [loc.location_id for loc in result] == ["shared-real"]

    def test_keeps_all_when_no_location_gateways_a_device(self) -> None:
        # Arlo sometimes omits gatewayDeviceIds entirely — don't strand the
        # account; keep everything so the coordinator fallback still works.
        own = LocationInfo(locationId="own", gatewayDeviceIds=[])
        devices = [_device("SOLO", parent_id=None)]
        result = relevant_locations([own], devices)
        assert [loc.location_id for loc in result] == ["own"]

    def test_keeps_multiple_relevant_locations(self) -> None:
        a = LocationInfo(locationId="A", gatewayDeviceIds=["OWN_BASE-1"])
        b = LocationInfo(locationId="B", gatewayDeviceIds=["OWN_BASE-2"])
        devices = [
            _device("CAM1", parent_id="BASE-1"),
            _device("CAM2", parent_id="BASE-2"),
        ]
        result = relevant_locations([a, b], devices)
        assert {loc.location_id for loc in result} == {"A", "B"}
