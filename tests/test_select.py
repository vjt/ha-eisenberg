"""Per-location security-mode selects (#16).

One SecurityModeSelect per location. Single-location accounts keep the legacy
unique_id (no entity orphaning on upgrade); multi-location accounts get one
scoped select each.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from custom_components.eisenberg.select import (
    SecurityModeSelect,
    build_mode_selects,
)
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


def _coord(locations: list[LocationState], devices: list[DeviceInfo] | None = None) -> Any:
    return SimpleNamespace(
        locations={loc.location_id: loc for loc in locations},
        devices=devices or [],
    )


class TestBuildModeSelects:
    def test_single_location_keeps_legacy_unique_id(self) -> None:
        coord = _coord(
            [LocationState(location_id="locA", gateway_device_ids=["BASE-1"])],
            [_device("BASE-1")],
        )
        selects = build_mode_selects(coord)
        assert len(selects) == 1
        assert selects[0].unique_id == "eisenberg_security_mode"

    def test_multi_location_one_scoped_select_each(self) -> None:
        a = LocationState(location_id="locA", location_name="Home", gateway_device_ids=["BASE-1"])
        b = LocationState(location_id="locB", location_name="Cabin", gateway_device_ids=["BASE-2"])
        coord = _coord([a, b], [_device("BASE-1"), _device("BASE-2")])
        selects = build_mode_selects(coord)
        assert {s.unique_id for s in selects} == {
            "eisenberg_security_mode_locA",
            "eisenberg_security_mode_locB",
        }


class TestSecurityModeSelectBehavior:
    def test_handle_update_reflects_its_location_mode(self) -> None:
        a = LocationState(location_id="locA", gateway_device_ids=[], active_mode="armHome")
        b = LocationState(location_id="locB", gateway_device_ids=[], active_mode="standby")
        coord = _coord([a, b])
        sel = SecurityModeSelect.__new__(SecurityModeSelect)
        sel.coordinator = coord  # type: ignore[attr-defined]
        sel._location_id = "locB"
        sel.async_write_ha_state = lambda: None  # type: ignore[method-assign]
        sel._handle_coordinator_update()
        assert sel._attr_current_option == "standby"

    async def test_select_option_sets_via_coordinator_with_location(self) -> None:
        calls: list[tuple[str, str]] = []

        async def _set(loc: str, mode: str) -> None:
            calls.append((loc, mode))

        coord = SimpleNamespace(
            locations={"locA": LocationState(location_id="locA", gateway_device_ids=[])},
            async_set_active_mode=_set,
        )
        sel = SecurityModeSelect.__new__(SecurityModeSelect)
        sel.coordinator = coord  # type: ignore[attr-defined]
        sel._location_id = "locA"
        await sel.async_select_option("armAway")
        assert calls == [("locA", "armAway")]
