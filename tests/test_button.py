"""Manual snapshot button (#8).

One SnapshotButton per camera. Press asks Arlo for a fresh full-frame
snapshot via the coordinator, which arrives later over MQTT and refreshes
the camera tile. Arlo refuses snapshots while the camera is disarmed
(standby) — the coordinator turns that into a loud HomeAssistantError.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.eisenberg.button import SnapshotButton
from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg import DeviceInfo


def _device(device_id: str) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Camera {device_id}",
            "modelId": "VMC2052A",
            "xCloudId": "CLOUD",
        }
    )


class TestSnapshotButton:
    def test_unique_id_and_device_scoped(self) -> None:
        btn = SnapshotButton(SimpleNamespace(), _device("CAM-1"))  # type: ignore[arg-type]
        assert btn.unique_id == "CAM-1_snapshot"
        assert btn.device_info["identifiers"] == {("eisenberg", "CAM-1")}

    async def test_press_delegates_to_coordinator(self) -> None:
        calls: list[str] = []

        async def _snap(device_id: str) -> None:
            calls.append(device_id)

        coord = SimpleNamespace(request_snapshot=_snap)
        btn = SnapshotButton.__new__(SnapshotButton)
        btn.coordinator = coord  # type: ignore[attr-defined]
        btn._device = _device("CAM-1")
        await btn.async_press()
        assert calls == ["CAM-1"]

    async def test_press_propagates_standby_error(self) -> None:
        async def _snap(device_id: str) -> None:
            raise HomeAssistantError("disarmed")

        coord = SimpleNamespace(request_snapshot=_snap)
        btn = SnapshotButton.__new__(SnapshotButton)
        btn.coordinator = coord  # type: ignore[attr-defined]
        btn._device = _device("CAM-1")
        with pytest.raises(HomeAssistantError):
            await btn.async_press()


class TestCoordinatorRequestSnapshot:
    """The standby-guard + session-retry now live on the coordinator so the
    camera service and the button share one code path."""

    async def test_refuses_when_standby(self) -> None:
        coord = SimpleNamespace(mode_for_device=lambda _d: "standby")
        with pytest.raises(HomeAssistantError, match="disarmed"):
            await EisenbergCoordinator.request_snapshot(coord, "CAM-1")  # type: ignore[arg-type]

    async def test_calls_client_when_armed(self) -> None:
        called: list[str] = []

        class _Client:
            async def request_snapshot(self, device_id: str) -> None:
                called.append(device_id)

        async def _retry(op_name: str, op: Any) -> None:
            await op()

        coord = SimpleNamespace(
            mode_for_device=lambda _d: "armHome",
            call_with_session_retry=_retry,
            client=_Client(),
        )
        await EisenbergCoordinator.request_snapshot(coord, "CAM-1")  # type: ignore[arg-type]
        assert called == ["CAM-1"]

    async def test_wraps_client_failure(self) -> None:
        async def _retry(op_name: str, op: Any) -> None:
            raise RuntimeError("boom")

        coord = SimpleNamespace(
            mode_for_device=lambda _d: "armHome",
            call_with_session_retry=_retry,
        )
        with pytest.raises(HomeAssistantError, match="Snapshot request failed"):
            await EisenbergCoordinator.request_snapshot(coord, "CAM-1")  # type: ignore[arg-type]
