"""Tests for EisenbergClient authentication flows."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from aiohttp import CookieJar
from aioresponses import aioresponses

from eisenberg.client import EisenbergClient
from eisenberg.exceptions import AuthenticationError, PushApprovalRequired


OCAPI = "https://ocapi-app.arlo.com"
MYAPI = "https://myapi.arlo.com"


def make_client(cookie_jar: CookieJar | None = None) -> EisenbergClient:
    return EisenbergClient(
        email="test@example.com",
        password="hunter2",
        device_id="test-device-uuid",
        cookie_jar=cookie_jar,
    )


class TestTrustedBrowserFlow:
    @pytest.fixture
    def mocked(self) -> aioresponses:
        with aioresponses() as m:
            # Step 1: auth
            m.post(f"{OCAPI}/api/auth", payload={
                "data": {
                    "token": "initial-token",
                    "userId": "USER-123",
                    "authCompleted": False,
                },
                "meta": {"code": 200},
            })
            # Step 2: getFactorId succeeds (browser trusted)
            m.post(f"{OCAPI}/api/getFactorId", payload={
                "data": {"factorId": "factor-abc"},
                "meta": {"code": 200},
            })
            # Step 3: startAuth completes instantly
            m.post(f"{OCAPI}/api/startAuth", payload={
                "data": {
                    "authCompleted": True,
                    "accessToken": {"token": "final-token"},
                },
                "meta": {"code": 200},
            })
            # Step 4: session
            m.get(f"{MYAPI}/hmsweb/users/session/v3", payload={
                "data": {"mqttUrl": "wss://mqtt.arlo.com:8084"},
                "success": True,
            })
            yield m

    async def test_trusted_login_returns_token(self, mocked: aioresponses) -> None:
        client = make_client()
        async with client:
            await client.login()
            assert client.token == "final-token"
            assert client.user_id == "USER-123"
            assert client.mqtt_url == "wss://mqtt.arlo.com:8084"

    async def test_trusted_login_no_push_needed(self, mocked: aioresponses) -> None:
        client = make_client()
        async with client:
            # Should NOT raise PushApprovalRequired
            await client.login()


class TestFirstTimeFlow:
    async def test_raises_push_approval_required(self) -> None:
        with aioresponses() as m:
            # Step 1: auth
            m.post(f"{OCAPI}/api/auth", payload={
                "data": {
                    "token": "initial-token",
                    "userId": "USER-123",
                    "authCompleted": False,
                },
                "meta": {"code": 200},
            })
            # Step 2: getFactorId FAILS (not trusted)
            m.post(f"{OCAPI}/api/getFactorId", payload={
                "data": {},
                "meta": {"code": 400, "error": "4012"},
            })
            # Step 3: startAuth returns factors
            m.post(f"{OCAPI}/api/startAuth", payload={
                "data": {
                    "factorAuthCode": "auth-code-xyz",
                    "factors": [
                        {"factorType": "PUSH", "displayName": "iPhone"},
                    ],
                    "MFA_Config": {"timeout": {"PUSH": 120}},
                },
                "meta": {"code": 200},
            })

            client = make_client()
            async with client:
                with pytest.raises(PushApprovalRequired) as exc_info:
                    await client.login()

                assert exc_info.value.factor_auth_code == "auth-code-xyz"
                assert len(exc_info.value.factors) == 1


class TestAuthFailure:
    async def test_bad_credentials_raises(self) -> None:
        with aioresponses() as m:
            m.post(f"{OCAPI}/api/auth", payload={
                "data": {},
                "meta": {"code": 401, "error": "1006"},
            })

            client = make_client()
            async with client:
                with pytest.raises(AuthenticationError):
                    await client.login()


class TestHeaders:
    def test_ocapi_headers_encode_token(self) -> None:
        client = make_client()
        headers = client._ocapi_headers("my-token")
        expected = base64.b64encode(b"my-token").decode()
        assert headers["Authorization"] == expected

    def test_myapi_headers_raw_token(self) -> None:
        client = make_client()
        client._x_cloud_id = "cloud-123"
        headers = client._myapi_headers("my-token")
        assert headers["Authorization"] == "my-token"
        assert headers["xCloudId"] == "cloud-123"
