"""Per-location mode tracking on the coordinator (#16).

Drives the coordinator's per-location API without standing up Home Assistant:
build a bare coordinator via __new__ and set just the attributes each method
touches. Mirrors the __new__ pattern in test_basestation_connectivity.py.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg import DeviceInfo
from eisenberg.models import LocationState, SubscribeOutcome, TopicResult

if TYPE_CHECKING:
    import pytest


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


def _coord(
    locations: list[LocationState], devices: list[DeviceInfo] | None = None
) -> EisenbergCoordinator:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord.locations = {loc.location_id: loc for loc in locations}
    coord._devices = devices or []
    coord.data = {}
    coord.async_set_updated_data = lambda *_a, **_k: None  # type: ignore[method-assign]
    return coord


class TestLocationForDevice:
    def test_single_location_unmatched_falls_back_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        loc = LocationState(location_id="locA", gateway_device_ids=[])  # no gateways
        coord = _coord([loc])
        with caplog.at_level(logging.WARNING):
            result = coord.location_for_device(_device("CAM", parent_id="BASE-X"))
        assert result is loc
        assert "matched no location" not in caplog.text  # silent on single location

    def test_multi_location_unmatched_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        a = LocationState(location_id="locA", gateway_device_ids=["BASE-1"])
        b = LocationState(location_id="locB", gateway_device_ids=["BASE-2"])
        coord = _coord([a, b])
        with caplog.at_level(logging.WARNING):
            result = coord.location_for_device(_device("CAM", parent_id="BASE-X"))
        assert result is a  # fallback to first
        assert "matched no location" in caplog.text

    def test_matched_returns_exact_location(self) -> None:
        a = LocationState(location_id="locA", gateway_device_ids=["BASE-1"])
        b = LocationState(location_id="locB", gateway_device_ids=["BASE-2"])
        coord = _coord([a, b])
        assert coord.location_for_device(_device("CAM", parent_id="BASE-2")) is b


class TestModeForDevice:
    def test_returns_devices_location_mode(self) -> None:
        a = LocationState(location_id="locA", gateway_device_ids=["BASE-1"], active_mode="armAway")
        b = LocationState(location_id="locB", gateway_device_ids=["BASE-2"], active_mode="standby")
        cam_a = _device("CAM-A", parent_id="BASE-1")
        cam_b = _device("CAM-B", parent_id="BASE-2")
        coord = _coord([a, b], [cam_a, cam_b])
        assert coord.mode_for_device("CAM-A") == "armAway"
        assert coord.mode_for_device("CAM-B") == "standby"

    def test_unknown_device_returns_none(self) -> None:
        coord = _coord([LocationState(location_id="locA", gateway_device_ids=[])])
        assert coord.mode_for_device("NOPE") is None


class TestApplyMode:
    def test_routes_to_named_location(self) -> None:
        a = LocationState(location_id="locA", gateway_device_ids=[], active_mode="standby")
        b = LocationState(location_id="locB", gateway_device_ids=[], active_mode="standby")
        coord = _coord([a, b])
        coord._apply_mode("locB", "armAway")
        assert a.active_mode == "standby"
        assert b.active_mode == "armAway"

    def test_no_location_id_single_location_updates_sole(self) -> None:
        a = LocationState(location_id="locA", gateway_device_ids=[], active_mode="standby")
        coord = _coord([a])
        coord._apply_mode(None, "armHome")
        assert a.active_mode == "armHome"

    def test_no_location_id_multi_location_is_ignored(self) -> None:
        a = LocationState(location_id="locA", gateway_device_ids=[], active_mode="standby")
        b = LocationState(location_id="locB", gateway_device_ids=[], active_mode="standby")
        coord = _coord([a, b])
        coord._apply_mode(None, "armAway")
        assert a.active_mode == "standby"
        assert b.active_mode == "standby"


class TestAsyncSetActiveMode:
    async def test_sets_mode_on_named_location_with_its_revision(self) -> None:
        a = LocationState(
            location_id="locA", gateway_device_ids=[], active_mode="standby", mode_revision=7
        )
        b = LocationState(
            location_id="locB", gateway_device_ids=[], active_mode="standby", mode_revision=3
        )
        coord = _coord([a, b])

        calls: list[tuple[str, str, int]] = []

        async def _set_active_mode(loc: str, mode: str, rev: int) -> Any:
            calls.append((loc, mode, rev))
            return SimpleNamespace(revision=rev + 1, properties=SimpleNamespace(mode=mode))

        coord.client = SimpleNamespace(set_active_mode=_set_active_mode)  # type: ignore[attr-defined]

        async def _passthrough(_name: str, op: Any) -> Any:
            return await op()

        coord.call_with_session_retry = _passthrough  # type: ignore[method-assign]

        await coord.async_set_active_mode("locB", "armAway")

        assert calls == [("locB", "armAway", 3)]  # used locB's revision, not locA's
        assert b.active_mode == "armAway"
        assert b.mode_revision == 4
        assert a.active_mode == "standby"  # untouched


class TestSubackSummary:
    def test_logs_counts_refused_and_user_topic(self, caplog: pytest.LogCaptureFixture) -> None:
        coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
        coord.client = SimpleNamespace(user_id="USER")  # type: ignore[attr-defined]
        outcome = SubscribeOutcome(
            results=[
                TopicResult(topic="d/A/out/#", code=0, granted=True),
                TopicResult(topic="d/B/out/#", code=0x80, granted=False),
                TopicResult(topic="u/USER/in/#", code=0x80, granted=False),
            ]
        )
        coord._mqtt = SimpleNamespace(subscribe_outcome=outcome)  # type: ignore[attr-defined]
        # Capture at DEBUG: the granted/refused counts and user-topic verdict
        # are INFO, but the refused-topics list is DEBUG (harmless partial
        # refusals shouldn't alarm at WARNING — reachable via HA's debug toggle).
        with caplog.at_level(logging.DEBUG):
            coord._log_subscribe_outcome()
        assert "1 granted, 2 refused of 3" in caplog.text
        assert "d/B/out/#" in caplog.text
        assert "u/USER/in/# = REFUSED" in caplog.text

    def test_user_topic_granted(self, caplog: pytest.LogCaptureFixture) -> None:
        coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
        coord.client = SimpleNamespace(user_id="USER")  # type: ignore[attr-defined]
        outcome = SubscribeOutcome(
            results=[TopicResult(topic="u/USER/in/#", code=1, granted=True)]
        )
        coord._mqtt = SimpleNamespace(subscribe_outcome=outcome)  # type: ignore[attr-defined]
        with caplog.at_level(logging.INFO):
            coord._log_subscribe_outcome()
        assert "u/USER/in/# = GRANTED" in caplog.text

    def test_no_outcome_is_noop(self) -> None:
        coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
        coord._mqtt = None  # type: ignore[attr-defined]
        coord._log_subscribe_outcome()  # must not raise
