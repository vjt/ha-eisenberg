"""Per-location security-mode selects (#16, #43).

One SecurityModeSelect per location, unique_id scoped by location id. A
single-location account used to get the bare legacy unique_id, so a second
Arlo account (a second config entry) collided with the first and lost its
select (#43). The legacy entity is migrated in the registry instead, so its
entity_id, history and automations survive.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from custom_components.eisenberg.select import (
    SecurityModeSelect,
    build_mode_selects,
    migrate_legacy_unique_id,
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
    def test_single_location_is_scoped_by_location(self) -> None:
        coord = _coord(
            [LocationState(location_id="locA", gateway_device_ids=["BASE-1"])],
            [_device("BASE-1")],
        )
        selects = build_mode_selects(coord)
        assert len(selects) == 1
        assert selects[0].unique_id == "eisenberg_security_mode_locA"
        assert selects[0].name == "Security mode"

    def test_two_accounts_do_not_collide(self) -> None:
        """#43: two config entries, one location each."""
        first = build_mode_selects(
            _coord([LocationState(location_id="locA", gateway_device_ids=[])])
        )
        second = build_mode_selects(
            _coord([LocationState(location_id="locB", gateway_device_ids=[])])
        )
        assert first[0].unique_id != second[0].unique_id

    def test_multi_location_one_scoped_select_each(self) -> None:
        a = LocationState(location_id="locA", location_name="Home", gateway_device_ids=["BASE-1"])
        b = LocationState(location_id="locB", location_name="Cabin", gateway_device_ids=["BASE-2"])
        coord = _coord([a, b], [_device("BASE-1"), _device("BASE-2")])
        selects = build_mode_selects(coord)
        assert {s.unique_id for s in selects} == {
            "eisenberg_security_mode_locA",
            "eisenberg_security_mode_locB",
        }

    def test_each_select_anchors_to_a_device_in_its_location(self) -> None:
        # Arlo sends gatewayDeviceIds as "{ownerId}_{deviceId}" (#39).
        a = LocationState(location_id="locA", gateway_device_ids=["O_BASE-1"])
        b = LocationState(location_id="locB", gateway_device_ids=["O_BASE-2"])
        cam_a = _device("CAM-A", parent_id="BASE-1")
        cam_b = _device("CAM-B", parent_id="BASE-2")
        coord = _coord([a, b], [cam_a, cam_b])
        anchors = {
            s.unique_id: next(iter(s.device_info["identifiers"]))[1]  # type: ignore[index]
            for s in build_mode_selects(coord)
        }
        assert anchors == {
            "eisenberg_security_mode_locA": "CAM-A",
            "eisenberg_security_mode_locB": "CAM-B",
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


class _Registry:
    """The slice of HA's entity registry the migration touches."""

    def __init__(self, entries: dict[str, tuple[str, str]]) -> None:
        # entity_id -> (unique_id, config_entry_id)
        self.entries = entries

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        assert (domain, platform) == ("select", "eisenberg")
        for entity_id, (uid, _) in self.entries.items():
            if uid == unique_id:
                return entity_id
        return None

    def async_get(self, entity_id: str) -> Any:
        uid, config_entry_id = self.entries[entity_id]
        return SimpleNamespace(unique_id=uid, config_entry_id=config_entry_id)

    def async_update_entity(self, entity_id: str, *, new_unique_id: str) -> None:
        _, config_entry_id = self.entries[entity_id]
        self.entries[entity_id] = (new_unique_id, config_entry_id)


LEGACY_ENTITY = "select.home_security_mode"


class TestLegacyMigration:
    def test_legacy_entity_moves_to_the_scoped_id(self) -> None:
        """Same entity_id, so history and automations survive the upgrade."""
        reg = _Registry({LEGACY_ENTITY: ("eisenberg_security_mode", "entry1")})
        migrate_legacy_unique_id(reg, "entry1", ["locA"])  # type: ignore[arg-type]
        assert reg.entries == {LEGACY_ENTITY: ("eisenberg_security_mode_locA", "entry1")}

    def test_another_entrys_legacy_entity_is_left_alone(self) -> None:
        reg = _Registry({LEGACY_ENTITY: ("eisenberg_security_mode", "entry1")})
        migrate_legacy_unique_id(reg, "entry2", ["locB"])  # type: ignore[arg-type]
        assert reg.entries == {LEGACY_ENTITY: ("eisenberg_security_mode", "entry1")}

    def test_nothing_to_migrate(self) -> None:
        reg = _Registry({})
        migrate_legacy_unique_id(reg, "entry1", ["locA"])  # type: ignore[arg-type]
        assert reg.entries == {}

    def test_multi_location_entry_is_left_alone(self) -> None:
        """The legacy id never belonged to a multi-location select: which
        location it meant is unknown, so it is not guessed."""
        reg = _Registry({LEGACY_ENTITY: ("eisenberg_security_mode", "entry1")})
        migrate_legacy_unique_id(reg, "entry1", ["locA", "locB"])  # type: ignore[arg-type]
        assert reg.entries == {LEGACY_ENTITY: ("eisenberg_security_mode", "entry1")}

    def test_scoped_id_already_taken_is_left_alone(self) -> None:
        reg = _Registry(
            {
                LEGACY_ENTITY: ("eisenberg_security_mode", "entry1"),
                "select.home_security_mode_2": ("eisenberg_security_mode_locA", "entry1"),
            }
        )
        migrate_legacy_unique_id(reg, "entry1", ["locA"])  # type: ignore[arg-type]
        assert reg.entries[LEGACY_ENTITY] == ("eisenberg_security_mode", "entry1")
