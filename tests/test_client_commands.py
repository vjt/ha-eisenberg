"""Tests for EisenbergClient device-command methods."""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses

from eisenberg.client import EisenbergClient
from eisenberg.exceptions import APIError, SessionExpiredError

MYAPI = "https://myapi.arlo.com"


def make_authed_client() -> EisenbergClient:
    client = EisenbergClient(
        email="test@example.com",
        password="hunter2",
        device_id="test-device-uuid",
    )
    client.token = "tok"
    client.user_id = "USER-123"
    client._x_cloud_id = "XCLOUD-1"
    # Per-device base-station xCloudIds, as populated by get_devices().
    client._device_cloud_ids = {"CAM-1": "XCLOUD-1"}
    # CAM-1 is base-less in this fixture: its own id is its gateway.
    client._device_parent_ids = {"CAM-1": "CAM-1"}
    return client


class TestSetSpotlight:
    async def test_on_with_intensity_sends_correct_body(self) -> None:
        with aioresponses() as m:
            m.post(
                f"{MYAPI}/hmsweb/users/devices/notify/CAM-1",
                payload={"success": True},
            )
            async with make_authed_client() as client:
                await client.set_spotlight("CAM-1", on=True, intensity=80)

            req = next(iter(m.requests.values()))[0]
            body = (
                json.loads(req.kwargs["json"])
                if isinstance(req.kwargs.get("json"), str)
                else req.kwargs["json"]
            )
            assert body["action"] == "set"
            assert body["resource"] == "cameras/CAM-1"
            assert body["properties"] == {"spotlight": {"enabled": True, "intensity": 80}}

    async def test_off_omits_intensity(self) -> None:
        with aioresponses() as m:
            m.post(
                f"{MYAPI}/hmsweb/users/devices/notify/CAM-1",
                payload={"success": True},
            )
            async with make_authed_client() as client:
                await client.set_spotlight("CAM-1", on=False)

            req = next(iter(m.requests.values()))[0]
            body = req.kwargs["json"]
            assert body["properties"] == {"spotlight": {"enabled": False}}

    async def test_failure_raises(self) -> None:
        with aioresponses() as m:
            m.post(
                f"{MYAPI}/hmsweb/users/devices/notify/CAM-1",
                payload={"success": False, "data": {"error": "device_offline"}},
            )
            async with make_authed_client() as client:
                with pytest.raises(APIError):
                    await client.set_spotlight("CAM-1", on=True)


class TestSessionExpired:
    """Arlo error 2015 ("Invalid Token") must surface as SessionExpiredError.

    This is the signal the coordinator catches to trigger a silent
    relogin-and-retry. A generic APIError would skip that path and let
    the failure bubble straight to the UI, which is what issue #2 was.
    """

    async def test_2015_raises_session_expired(self) -> None:
        with aioresponses() as m:
            m.post(
                f"{MYAPI}/hmsweb/users/devices/startStream",
                payload={
                    "success": False,
                    "data": {
                        "error": "2015",
                        "message": "Your session has expired. Please log in.",
                        "reason": "Invalid Token",
                    },
                },
            )
            async with make_authed_client() as client:
                with pytest.raises(SessionExpiredError):
                    await client.start_stream("CAM-1")

    async def test_other_error_raises_api_error(self) -> None:
        with aioresponses() as m:
            m.post(
                f"{MYAPI}/hmsweb/users/devices/notify/CAM-1",
                payload={"success": False, "data": {"error": "4006"}},
            )
            async with make_authed_client() as client:
                with pytest.raises(APIError) as excinfo:
                    await client.set_siren("CAM-1", on=True)
                assert excinfo.value.code == "4006"
                assert not isinstance(excinfo.value, SessionExpiredError)


class TestPerDeviceCloudId:
    """Per-device xCloudId routing (issue #7).

    Each camera carries its own base-station xCloudId. Accounts with cameras
    on multiple Arlo bases have a different xCloudId per camera; sending the
    wrong base's id gets the request rejected with "no such device". So every
    per-device REST call must send that device's own xCloudId — not the first
    device's, which is what broke streaming on multi-base accounts.
    """

    @staticmethod
    def _devices_payload() -> dict:
        return {
            "success": True,
            "data": [
                {
                    "deviceId": "CAM-A",
                    "deviceName": "Front",
                    "modelId": "VMC2052A",
                    "xCloudId": "BASE-A",
                },
                {
                    "deviceId": "CAM-B",
                    "deviceName": "Back",
                    "modelId": "VMC2052A",
                    "xCloudId": "BASE-B",
                },
            ],
        }

    @staticmethod
    def _post_header(m: aioresponses, key: str) -> str:
        reqs = [calls for (method, _url), calls in m.requests.items() if method == "POST"]
        return reqs[0][0].kwargs["headers"][key]

    async def test_get_devices_maps_each_device_to_its_own_cloud_id(self) -> None:
        with aioresponses() as m:
            m.get(f"{MYAPI}/hmsweb/v2/users/devices", payload=self._devices_payload())
            async with make_authed_client() as client:
                client._device_cloud_ids = {}
                await client.get_devices()

            assert client._device_cloud_ids == {"CAM-A": "BASE-A", "CAM-B": "BASE-B"}

    async def test_start_stream_sends_target_device_cloud_id(self) -> None:
        with aioresponses() as m:
            m.get(f"{MYAPI}/hmsweb/v2/users/devices", payload=self._devices_payload())
            m.post(
                f"{MYAPI}/hmsweb/users/devices/startStream",
                payload={"success": True, "data": {"url": "rtsp://stream"}},
            )
            async with make_authed_client() as client:
                client._device_cloud_ids = {}
                await client.get_devices()
                # CAM-B is NOT the first device — the old bug sent BASE-A here.
                await client.start_stream("CAM-B")

            assert self._post_header(m, "xCloudId") == "BASE-B"

    async def test_command_for_undiscovered_device_raises(self) -> None:
        async with make_authed_client() as client:
            client._device_cloud_ids = {}
            with pytest.raises(RuntimeError, match="Unknown device_id"):
                await client.set_siren("CAM-X", on=True)


class TestBaseStationRouting:
    """Per-device commands must address the base station, not the camera (#16).

    Arlo routes device commands through the controlling gateway. A camera
    living under a real base station (parentId != deviceId) rejects a request
    addressed to its own id with "Invalid camera activity state change" (4006).
    Base-less cameras are their own gateway, so they keep targeting their id.
    """

    @staticmethod
    def _devices_payload() -> dict:
        # CAM lives under BASE; SOLO is base-less (no parentId).
        return {
            "success": True,
            "data": [
                {
                    "deviceId": "BASE",
                    "deviceName": "Base",
                    "modelId": "VMB5000",
                    "xCloudId": "XC-BASE",
                },
                {
                    "deviceId": "CAM",
                    "deviceName": "Front",
                    "modelId": "VMC2052A",
                    "xCloudId": "XC-BASE",
                    "parentId": "BASE",
                },
                {
                    "deviceId": "SOLO",
                    "deviceName": "Garden",
                    "modelId": "VMC2052A",
                    "xCloudId": "XC-SOLO",
                },
            ],
        }

    @staticmethod
    def _post(m: aioresponses) -> tuple[str, dict]:
        for (method, url), calls in m.requests.items():
            if method == "POST":
                req = calls[0]
                body = req.kwargs["json"]
                body = json.loads(body) if isinstance(body, str) else body
                return str(url), body
        raise AssertionError("no POST recorded")

    async def _discover(self, m: aioresponses, client: EisenbergClient) -> None:
        m.get(f"{MYAPI}/hmsweb/v2/users/devices", payload=self._devices_payload())
        client._device_cloud_ids = {}
        client._device_parent_ids = {}
        await client.get_devices()

    async def test_get_devices_maps_camera_to_its_base(self) -> None:
        with aioresponses() as m:
            async with make_authed_client() as client:
                await self._discover(m, client)
            assert client._device_parent_ids == {
                "BASE": "BASE",
                "CAM": "BASE",
                "SOLO": "SOLO",
            }

    async def test_snapshot_targets_base_via_dedicated_endpoint(self) -> None:
        with aioresponses() as m:
            async with make_authed_client() as client:
                await self._discover(m, client)
                m.post(
                    f"{MYAPI}/hmsweb/users/devices/fullFrameSnapshot",
                    payload={"success": True},
                )
                await client.request_snapshot("CAM")

            url, body = self._post(m)
            assert url.endswith("/hmsweb/users/devices/fullFrameSnapshot")
            assert body["to"] == "BASE"
            assert body["resource"] == "cameras/CAM"
            assert body["properties"] == {"activityState": "fullFrameSnapshot"}

    async def test_base_less_snapshot_targets_itself(self) -> None:
        with aioresponses() as m:
            async with make_authed_client() as client:
                await self._discover(m, client)
                m.post(
                    f"{MYAPI}/hmsweb/users/devices/fullFrameSnapshot",
                    payload={"success": True},
                )
                await client.request_snapshot("SOLO")

            _url, body = self._post(m)
            assert body["to"] == "SOLO"
            assert body["resource"] == "cameras/SOLO"

    async def test_spotlight_targets_base(self) -> None:
        with aioresponses() as m:
            async with make_authed_client() as client:
                await self._discover(m, client)
                m.post(f"{MYAPI}/hmsweb/users/devices/notify/BASE", payload={"success": True})
                await client.set_spotlight("CAM", on=True)

            url, body = self._post(m)
            assert url.endswith("/notify/BASE")
            assert body["to"] == "BASE"
            assert body["resource"] == "cameras/CAM"

    async def test_siren_targets_base(self) -> None:
        with aioresponses() as m:
            async with make_authed_client() as client:
                await self._discover(m, client)
                m.post(f"{MYAPI}/hmsweb/users/devices/notify/BASE", payload={"success": True})
                await client.set_siren("CAM", on=True)

            url, body = self._post(m)
            assert url.endswith("/notify/BASE")
            assert body["to"] == "BASE"
            assert body["resource"] == "siren/CAM"
