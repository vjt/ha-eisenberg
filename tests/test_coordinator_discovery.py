"""What the log says about how an account is laid out (#29).

`jbarbash` opened an issue quoting one of our own lines back at us:

    Account spans 3 Arlo base stations — subscribing to all. If any device
    entities stay unknown, verify their xCloudId is in the subscribed list
    above.

    I have no base stations, just individual cameras.

He was right. The line counted distinct xCloudIds and called them base
stations — but a base-less camera is its own gateway with its own xCloudId,
so three cameras and no hub produced a WARNING announcing three base
stations, plus an instruction to go and verify something that was never
wrong. Spanning several gateways is the normal shape of both kinds of
account, so it is worth stating and not worth warning about.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg import DeviceInfo

if TYPE_CHECKING:
    import pytest


def _camera(device_id: str, cloud: str) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Camera {device_id}",
            "modelId": "VMC2052A",
            "deviceType": "camera",
            "xCloudId": cloud,
            "parentId": device_id,
        }
    )


def _base(device_id: str, cloud: str) -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": device_id,
            "deviceName": f"Base {device_id}",
            "modelId": "VMB4000",
            "deviceType": "basestation",
            "xCloudId": cloud,
            "parentId": device_id,
        }
    )


def _coordinator(devices: list[DeviceInfo]) -> EisenbergCoordinator:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord._devices = devices
    return coord


class TestGatewaySpanLog:
    def test_base_less_account_is_not_told_it_has_base_stations(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#29 as reported: three cameras, no hub, three xCloudIds."""
        coord = _coordinator(
            [_camera("CAM_A", "CLOUD_A"), _camera("CAM_B", "CLOUD_B"), _camera("CAM_C", "CLOUD_C")]
        )
        with caplog.at_level(logging.DEBUG):
            coord._log_gateway_span()
        assert "base station" not in caplog.text.lower()
        assert "3" in caplog.text

    def test_a_healthy_account_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """Nothing is wrong, so nothing needs the user's attention."""
        coord = _coordinator([_camera("CAM_A", "CLOUD_A"), _camera("CAM_B", "CLOUD_B")])
        with caplog.at_level(logging.WARNING):
            coord._log_gateway_span()
        assert caplog.records == []

    def test_the_span_is_still_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        """It stays in the log — it is the first thing worth knowing when a
        device's events never arrive."""
        coord = _coordinator([_camera("CAM_A", "CLOUD_A"), _camera("CAM_B", "CLOUD_B")])
        with caplog.at_level(logging.INFO):
            coord._log_gateway_span()
        assert "gateway" in caplog.text.lower()

    def test_a_base_stationed_account_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        """Where hubs do exist, naming them is the useful diagnostic."""
        coord = _coordinator(
            [
                _base("BASE_A", "CLOUD_A"),
                _camera("CAM_A", "CLOUD_A"),
                _base("BASE_B", "CLOUD_B"),
            ]
        )
        with caplog.at_level(logging.INFO):
            coord._log_gateway_span()
        assert "2 base station" in caplog.text.lower()

    def test_a_single_gateway_is_not_worth_a_line(self, caplog: pytest.LogCaptureFixture) -> None:
        coord = _coordinator([_camera("CAM_A", "CLOUD_A")])
        with caplog.at_level(logging.DEBUG):
            coord._log_gateway_span()
        assert caplog.records == []
