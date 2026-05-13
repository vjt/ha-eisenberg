"""Tests for EisenbergClient device-command methods."""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses

from eisenberg.client import EisenbergClient
from eisenberg.exceptions import APIError

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
