# tests/test_config_flow.py
"""Tests for the form-driven push approval flow.

These exercise EisenbergClient.try_finish_auth and start_push_login,
which the config flow drives one-call-per-form-submit. We can't spin up
the full HA config flow in this repo (pytest-homeassistant-custom-component
is not a dep), so we cover the I/O-bearing surface that the flow calls into.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest
from aioresponses import aioresponses

if TYPE_CHECKING:
    from aiohttp import CookieJar

from eisenberg.client import EisenbergClient
from eisenberg.exceptions import (
    AuthenticationError,
    PushApprovalRequired,
    RateLimitedError,
)

OCAPI = "https://ocapi-app.arlo.com"
MYAPI = "https://myapi.arlo.com"


def make_client(cookie_jar: CookieJar | None = None) -> EisenbergClient:
    return EisenbergClient(
        email="test@example.com",
        password="hunter2",
        device_id="test-device-uuid",
        cookie_jar=cookie_jar,
    )


def _stub_initial_login(m: aioresponses) -> None:
    """Stub an /api/auth + /api/getFactorId that lands on PushApprovalRequired."""
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


class TestTryFinishAuth:
    """Single-shot finishAuth: each call costs one rate-limit token."""

    async def test_returns_true_on_completion(self) -> None:
        with aioresponses() as m:
            _stub_initial_login(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {"factorAuthCode": "auth-code-xyz"},
                    "meta": {"code": 200},
                },
            )
            m.post(
                f"{OCAPI}/api/finishAuth",
                payload={
                    "data": {
                        "authCompleted": True,
                        "token": "final-token",
                        "browserAuthCode": "browser-pair-code",
                    },
                    "meta": {"code": 200},
                },
            )
            m.post(
                f"{OCAPI}/api/startPairingFactor",
                payload={"data": {}, "meta": {"code": 200}},
            )
            m.get(
                f"{MYAPI}/hmsweb/users/session/v3",
                payload={
                    "data": {"mqttUrl": "wss://mqtt.arlo.com:8084"},
                    "success": True,
                },
            )

            client = make_client()
            async with client:
                with pytest.raises(PushApprovalRequired):
                    await client.login()
                code = await client.start_push_login()

                approved = await client.try_finish_auth(code)
                assert approved is True
                assert client.token == "final-token"
                assert client.mqtt_url == "wss://mqtt.arlo.com:8084"

    async def test_returns_false_when_pending(self) -> None:
        """Push not approved yet — return False, do NOT raise."""
        with aioresponses() as m:
            _stub_initial_login(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {"factorAuthCode": "auth-code-xyz"},
                    "meta": {"code": 200},
                },
            )
            m.post(
                f"{OCAPI}/api/finishAuth",
                payload={
                    "data": {"authCompleted": False},
                    "meta": {
                        "code": 401,
                        "message": "Authentication is not finished yet",
                    },
                },
            )

            client = make_client()
            async with client:
                with pytest.raises(PushApprovalRequired):
                    await client.login()
                code = await client.start_push_login()

                approved = await client.try_finish_auth(code)
                assert approved is False

    async def test_raises_rate_limit(self) -> None:
        """`Too many requests` must raise RateLimitedError, not False."""
        with aioresponses() as m:
            _stub_initial_login(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {"factorAuthCode": "auth-code-xyz"},
                    "meta": {"code": 200},
                },
            )
            m.post(
                f"{OCAPI}/api/finishAuth",
                payload={
                    "data": {},
                    "meta": {"code": 429, "message": "Too many requests"},
                },
            )

            client = make_client()
            async with client:
                with pytest.raises(PushApprovalRequired):
                    await client.login()
                code = await client.start_push_login()

                with pytest.raises(RateLimitedError):
                    await client.try_finish_auth(code)

    async def test_raises_on_expired_push(self) -> None:
        """Any other failure → AuthenticationError so the flow can abort."""
        with aioresponses() as m:
            _stub_initial_login(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {"factorAuthCode": "auth-code-xyz"},
                    "meta": {"code": 200},
                },
            )
            m.post(
                f"{OCAPI}/api/finishAuth",
                payload={
                    "data": {},
                    "meta": {"code": 400, "message": "Code expired"},
                },
            )

            client = make_client()
            async with client:
                with pytest.raises(PushApprovalRequired):
                    await client.login()
                code = await client.start_push_login()

                with pytest.raises(AuthenticationError):
                    await client.try_finish_auth(code)


class TestStartPushLoginRateLimit:
    async def test_start_push_login_raises_rate_limit(self) -> None:
        with aioresponses() as m:
            _stub_initial_login(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {},
                    "meta": {"code": 429, "message": "Too many requests"},
                },
            )

            client = make_client()
            async with client:
                with pytest.raises(PushApprovalRequired):
                    await client.login()
                with pytest.raises(RateLimitedError):
                    await client.start_push_login()


class TestPushPayloadEncoding:
    """The /api/auth payload encodes password as base64."""

    async def test_auth_body_uses_b64_password(self) -> None:
        with aioresponses() as m:
            _stub_initial_login(m)

            client = make_client()
            async with client:
                with pytest.raises(PushApprovalRequired):
                    await client.login()

            # aioresponses records request kwargs per (method, URL) tuple.
            # Find the auth call and check its JSON body.
            auth_calls = [
                call
                for (method, url), calls in m.requests.items()
                if method == "POST" and "/api/auth" in str(url) and "/getFactorId" not in str(url)
                for call in calls
            ]
            assert auth_calls, "expected at least one /api/auth call"
            body = auth_calls[0].kwargs["json"]
            assert body["password"] == base64.b64encode(b"hunter2").decode()
