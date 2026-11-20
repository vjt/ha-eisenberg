"""Keeping the base-station event registration alive (issue #45).

A base station publishes to this session only while it holds our registration
(issue #27), and the registration lapses within about two minutes. jakeb1221
timed snapshot requests on a VMB4000 against the renewals in his debug log:
answered 34 s and 92 s after a renewal, never from 161 s on. With the
registration renewed only on the 25-minute health tick, a snapshot request was
lost for roughly 28 minutes out of every 30, while motion, battery and signal,
which do not need the registration, kept arriving and hid it.

pyaarlo re-registers every 60 seconds (`FAST_REFRESH_INTERVAL` →
`_ping_bases` → `ArloBase.ping()`); so does the integration now, on a timer of
its own, independent of the health tick.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from custom_components.eisenberg import coordinator as coordinator_module
from custom_components.eisenberg.coordinator import (
    BASE_STATION_PING_INTERVAL,
    EisenbergCoordinator,
)
from eisenberg import DeviceInfo

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

BASE = "5GP59A7RA0411"
OTHER_BASE = "5GP59A7RA0999"
CAM = "5GG39A71A13BD"
CLOUD = "A92ZBCN6-2400-115-112185863"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

# The longest gap after a renewal at which the VMB4000 still answered (#45).
LAST_ANSWERED_AFTER = timedelta(seconds=92)


def _device(device_id: str, device_type: str, parent_id: str) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Device {device_id}",
            "modelId": "VMC4030" if device_type == "camera" else "VMB4000",
            "deviceType": device_type,
            "xCloudId": CLOUD,
            "parentId": parent_id,
        }
    )


class _Client:
    """Client boundary: records what each base was asked, can refuse."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.refuse: set[str] = set()

    async def register_event_subscription(self, base_id: str) -> None:
        self.calls.append(f"register:{base_id}")
        if base_id in self.refuse:
            raise RuntimeError("Arlo said no")

    async def request_device_states(self, base_id: str) -> None:
        self.calls.append(f"pull:{base_id}")


class _Entry:
    def __init__(self) -> None:
        self.on_unload: list[Callable[[], None]] = []

    def async_on_unload(self, func: Callable[[], None]) -> None:
        self.on_unload.append(func)


def _coordinator(devices: list[DeviceInfo]) -> tuple[Any, _Client, _Entry]:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord._devices = devices
    coord.device_states = {}
    coord.basestation_connection = {}
    coord.data = {}
    coord._push_to_entities = lambda: None  # type: ignore[method-assign]
    coord.hass = SimpleNamespace()  # type: ignore[assignment]
    client = _Client()
    coord.client = client  # type: ignore[assignment]
    entry = _Entry()
    coord.entry = entry  # type: ignore[assignment]
    return coord, client, entry


def _hub_account() -> list[DeviceInfo]:
    return [
        _device(BASE, "siren", BASE),
        _device(OTHER_BASE, "basestation", OTHER_BASE),
        _device(CAM, "camera", BASE),
    ]


class TestPingInterval:
    def test_renews_before_the_registration_lapses(self) -> None:
        assert BASE_STATION_PING_INTERVAL < LAST_ANSWERED_AFTER


class TestPing:
    async def test_registers_with_every_base_station(self) -> None:
        coord, client, _ = _coordinator(_hub_account())
        await coord._ping_base_stations(NOW)
        assert sorted(client.calls) == [f"register:{BASE}", f"register:{OTHER_BASE}"]

    async def test_does_not_pull_device_states(self) -> None:
        """The pull stays on the health tick: battery and signal change
        slowly, the registration is what lapses."""
        coord, client, _ = _coordinator(_hub_account())
        await coord._ping_base_stations(NOW)
        assert not any(call.startswith("pull:") for call in client.calls)

    async def test_base_less_account_pings_nobody(self) -> None:
        coord, client, _ = _coordinator([_device(CAM, "camera", CAM)])
        await coord._ping_base_stations(NOW)
        assert client.calls == []

    async def test_refusal_marks_the_base_unavailable(self) -> None:
        coord, client, _ = _coordinator(_hub_account())
        client.refuse.add(BASE)
        await coord._ping_base_stations(NOW)
        assert coord.basestation_connection[BASE] == "unavailable"
        assert coord.basestation_connection[OTHER_BASE] == "available"

    async def test_recovery_marks_the_base_available(self) -> None:
        coord, client, _ = _coordinator(_hub_account())
        client.refuse.add(BASE)
        await coord._ping_base_stations(NOW)
        client.refuse.clear()
        await coord._ping_base_stations(NOW)
        assert coord.basestation_connection[BASE] == "available"

    async def test_a_refusing_base_warns_once_not_every_minute(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        coord, client, _ = _coordinator(_hub_account())
        client.refuse.add(BASE)
        for _ in range(3):
            await coord._ping_base_stations(NOW)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert BASE in warnings[0].getMessage()


class TestPingTimer:
    def _track(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[tuple[Callable[..., Any], timedelta]]:
        scheduled: list[tuple[Callable[..., Any], timedelta]] = []

        def _fake_track(
            hass: Any, action: Callable[..., Any], interval: timedelta, **_: Any
        ) -> Callable[[], None]:
            scheduled.append((action, interval))
            return lambda: None

        monkeypatch.setattr(coordinator_module, "async_track_time_interval", _fake_track)
        return scheduled

    def test_hub_account_pings_on_its_own_timer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scheduled = self._track(monkeypatch)
        coord, _, entry = _coordinator(_hub_account())
        coord._start_base_station_ping()
        assert len(scheduled) == 1
        action, interval = scheduled[0]
        assert interval == BASE_STATION_PING_INTERVAL
        assert action == coord._ping_base_stations
        # Cancelled with the entry, including a setup that fails later.
        assert len(entry.on_unload) == 1

    def test_base_less_account_schedules_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scheduled = self._track(monkeypatch)
        coord, _, entry = _coordinator([_device(CAM, "camera", CAM)])
        coord._start_base_station_ping()
        assert scheduled == []
        assert entry.on_unload == []
