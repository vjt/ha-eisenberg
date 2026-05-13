"""Tests for EisenbergClient authentication flows."""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING

import pytest
from aioresponses import aioresponses

if TYPE_CHECKING:
    from aiohttp import CookieJar

from eisenberg.client import EisenbergClient
from eisenberg.exceptions import AuthenticationError, MfaRequired
from eisenberg.models import FactorType

OCAPI = "https://ocapi-app.arlo.com"
MYAPI = "https://myapi.arlo.com"

# /api/getFactors carries a cache-busting `data=<epoch>` query param, so all
# stubs of that endpoint must register against a URL regex rather than a
# fixed string.
GET_FACTORS_RE = re.compile(rf"^{re.escape(OCAPI)}/api/getFactors.*")


def make_client(cookie_jar: CookieJar | None = None) -> EisenbergClient:
    return EisenbergClient(
        email="test@example.com",
        password="hunter2",
        device_id="test-device-uuid",
        cookie_jar=cookie_jar,
    )


_TWO_FACTORS_PAYLOAD = {
    "data": {
        "items": [
            {
                "factorId": "fid-push",
                "factorType": "PUSH",
                "displayName": "iPhone 16",
                "factorNickname": "iPhone 16",
                "factorRole": "PRIMARY",
            },
            {
                "factorId": "fid-email",
                "factorType": "EMAIL",
                "displayName": "test@example.com",
                "factorNickname": "test@example.com",
                "factorRole": "SECONDARY",
            },
        ],
    },
    "meta": {"code": 200},
}


class TestTrustedBrowserFlow:
    @pytest.fixture
    def mocked(self) -> aioresponses:
        with aioresponses() as m:
            # Step 1: auth
            m.post(
                f"{OCAPI}/api/auth",
                payload={
                    "data": {
                        "token": "initial-token",
                        "userId": "USER-123",
                        "authCompleted": False,
                    },
                    "meta": {"code": 200},
                },
            )
            # Step 2: getFactorId succeeds (browser trusted)
            m.post(
                f"{OCAPI}/api/getFactorId",
                payload={
                    "data": {"factorId": "factor-abc"},
                    "meta": {"code": 200},
                },
            )
            # Step 3: startAuth completes instantly
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {
                        "authCompleted": True,
                        "accessToken": {"token": "final-token"},
                    },
                    "meta": {"code": 200},
                },
            )
            # Step 4: session
            m.get(
                f"{MYAPI}/hmsweb/users/session/v3",
                payload={
                    "data": {"mqttUrl": "wss://mqtt.arlo.com:8084"},
                    "success": True,
                },
            )
            yield m

    async def test_trusted_login_returns_token(self, mocked: aioresponses) -> None:
        client = make_client()
        async with client:
            await client.login()
            assert client.token == "final-token"
            assert client.user_id == "USER-123"
            assert client.mqtt_url == "wss://mqtt.arlo.com:8084"

    async def test_trusted_login_no_mfa(self, mocked: aioresponses) -> None:
        client = make_client()
        async with client:
            # Should NOT raise MfaRequired
            await client.login()


class TestMfaDiscovery:
    async def test_login_raises_mfa_required_with_factor_list(self) -> None:
        """login() must hit getFactors and signal MFA without firing anything."""
        with aioresponses() as m:
            m.post(
                f"{OCAPI}/api/auth",
                payload={
                    "data": {
                        "token": "initial-token",
                        "userId": "USER-123",
                        "authCompleted": False,
                    },
                    "meta": {"code": 200},
                },
            )
            m.post(
                f"{OCAPI}/api/getFactorId",
                payload={
                    "data": {},
                    "meta": {"code": 400, "error": "4012"},
                },
            )
            m.get(GET_FACTORS_RE, payload=_TWO_FACTORS_PAYLOAD)

            client = make_client()
            async with client:
                with pytest.raises(MfaRequired) as exc_info:
                    await client.login()

                factors = exc_info.value.factors
                assert {f.factor_type for f in factors} == {
                    FactorType.PUSH,
                    FactorType.EMAIL,
                }
                # Token was stashed for start_mfa() use
                assert client.token == "initial-token"
                assert client.user_id == "USER-123"

            # Crucially: no startAuth call was made (discovery is side-effect-free)
            requests = [k for k, _ in m.requests]
            assert all("/api/startAuth" not in str(req) for req in requests)


class TestStartMfa:
    async def _login_until_mfa(self, m: aioresponses) -> EisenbergClient:
        m.post(
            f"{OCAPI}/api/auth",
            payload={
                "data": {
                    "token": "initial-token",
                    "userId": "USER-123",
                    "authCompleted": False,
                },
                "meta": {"code": 200},
            },
        )
        m.post(
            f"{OCAPI}/api/getFactorId",
            payload={"data": {}, "meta": {"code": 400, "error": "4012"}},
        )
        m.get(GET_FACTORS_RE, payload=_TWO_FACTORS_PAYLOAD)
        client = make_client()
        await client.__aenter__()
        with pytest.raises(MfaRequired):
            await client.login()
        return client

    async def test_push_factor_uses_empty_factor_type(self) -> None:
        """PingOne PUSH startAuth wants factorType="" with factorId."""
        from eisenberg.models import SecondFactor

        with aioresponses() as m:
            client = await self._login_until_mfa(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {"factorAuthCode": "code-push"},
                    "meta": {"code": 200},
                },
            )
            try:
                push_factor = SecondFactor.model_validate(_TWO_FACTORS_PAYLOAD["data"]["items"][0])
                code = await client.start_mfa(push_factor)
                assert code == "code-push"

                start_calls = [
                    call
                    for (method, url), calls in m.requests.items()
                    if method == "POST" and "/api/startAuth" in str(url)
                    for call in calls
                ]
                assert start_calls
                body = start_calls[0].kwargs["json"]
                assert body["factorType"] == ""
                assert body["factorId"] == "fid-push"
            finally:
                await client.__aexit__(None, None, None)

    async def test_email_factor_uses_browser_factor_type(self) -> None:
        """PingOne EMAIL startAuth wants factorType="BROWSER" with factorId."""
        from eisenberg.models import SecondFactor

        with aioresponses() as m:
            client = await self._login_until_mfa(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {"factorAuthCode": "code-email"},
                    "meta": {"code": 200},
                },
            )
            try:
                email_factor = SecondFactor.model_validate(
                    _TWO_FACTORS_PAYLOAD["data"]["items"][1]
                )
                code = await client.start_mfa(email_factor)
                assert code == "code-email"

                start_calls = [
                    call
                    for (method, url), calls in m.requests.items()
                    if method == "POST" and "/api/startAuth" in str(url)
                    for call in calls
                ]
                body = start_calls[0].kwargs["json"]
                assert body["factorType"] == "BROWSER"
                assert body["factorId"] == "fid-email"
            finally:
                await client.__aexit__(None, None, None)


class TestAuthFailure:
    async def test_bad_credentials_raises(self) -> None:
        with aioresponses() as m:
            m.post(
                f"{OCAPI}/api/auth",
                payload={
                    "data": {},
                    "meta": {"code": 401, "error": "1006"},
                },
            )

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
