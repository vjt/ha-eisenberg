# tests/test_config_flow.py
"""Tests for the form-driven MFA flow.

These exercise EisenbergClient.start_mfa and try_finish_auth (both PUSH and
EMAIL variants), which the config flow drives one-call-per-form-submit. We
can't spin up the full HA config flow in this repo (pytest-homeassistant-
custom-component is not a dep), so we cover the I/O-bearing surface that
the flow calls into.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING

import pytest
from aioresponses import aioresponses

if TYPE_CHECKING:
    from aiohttp import CookieJar

from eisenberg.client import EisenbergClient
from eisenberg.exceptions import (
    AuthenticationError,
    MfaRequired,
    RateLimitedError,
)
from eisenberg.models import SecondFactor

OCAPI = "https://ocapi-app.arlo.com"
MYAPI = "https://myapi.arlo.com"

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


def _push_factor() -> SecondFactor:
    return SecondFactor.model_validate(_TWO_FACTORS_PAYLOAD["data"]["items"][0])


def _email_factor() -> SecondFactor:
    return SecondFactor.model_validate(_TWO_FACTORS_PAYLOAD["data"]["items"][1])


def _stub_until_mfa(m: aioresponses) -> None:
    """Stub auth + getFactorId-rejected + getFactors so login() raises MfaRequired."""
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


class TestTryFinishAuthPush:
    """Single-shot finishAuth: each call costs one rate-limit token."""

    async def test_returns_true_on_completion(self) -> None:
        with aioresponses() as m:
            _stub_until_mfa(m)
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
                with pytest.raises(MfaRequired):
                    await client.login()
                code = await client.start_mfa(_push_factor())

                approved = await client.try_finish_auth(code)
                assert approved is True
                assert client.token == "final-token"
                assert client.mqtt_url == "wss://mqtt.arlo.com:8084"

    async def test_returns_false_when_pending(self) -> None:
        """Push not approved yet — return False, do NOT raise."""
        with aioresponses() as m:
            _stub_until_mfa(m)
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
                with pytest.raises(MfaRequired):
                    await client.login()
                code = await client.start_mfa(_push_factor())

                approved = await client.try_finish_auth(code)
                assert approved is False

    async def test_raises_rate_limit(self) -> None:
        """`Too many requests` must raise RateLimitedError, not False."""
        with aioresponses() as m:
            _stub_until_mfa(m)
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
                with pytest.raises(MfaRequired):
                    await client.login()
                code = await client.start_mfa(_push_factor())

                with pytest.raises(RateLimitedError):
                    await client.try_finish_auth(code)

    async def test_raises_on_expired_push(self) -> None:
        """Any other failure → AuthenticationError so the flow can abort."""
        with aioresponses() as m:
            _stub_until_mfa(m)
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
                with pytest.raises(MfaRequired):
                    await client.login()
                code = await client.start_mfa(_push_factor())

                with pytest.raises(AuthenticationError):
                    await client.try_finish_auth(code)


class TestTryFinishAuthEmail:
    """EMAIL factor flow: finishAuth carries an `otp` field."""

    async def test_email_otp_sent_in_body(self) -> None:
        """finishAuth(..., otp=...) must include the otp in the request body."""
        with aioresponses() as m:
            _stub_until_mfa(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {"factorAuthCode": "email-code"},
                    "meta": {"code": 200},
                },
            )
            m.post(
                f"{OCAPI}/api/finishAuth",
                payload={
                    "data": {
                        "authCompleted": True,
                        "token": "final-token",
                    },
                    "meta": {"code": 200},
                },
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
                with pytest.raises(MfaRequired):
                    await client.login()
                code = await client.start_mfa(_email_factor())

                approved = await client.try_finish_auth(code, otp="123456")
                assert approved is True

            # Verify the finishAuth payload carried otp
            finish_calls = [
                call
                for (method, url), calls in m.requests.items()
                if method == "POST" and "/api/finishAuth" in str(url)
                for call in calls
            ]
            assert finish_calls
            body = finish_calls[0].kwargs["json"]
            assert body["otp"] == "123456"
            assert body["factorAuthCode"] == "email-code"
            assert body["isBrowserTrusted"] is True

    async def test_bad_otp_raises_auth_error(self) -> None:
        with aioresponses() as m:
            _stub_until_mfa(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {"factorAuthCode": "email-code"},
                    "meta": {"code": 200},
                },
            )
            m.post(
                f"{OCAPI}/api/finishAuth",
                payload={
                    "data": {},
                    "meta": {"code": 400, "message": "Invalid OTP"},
                },
            )

            client = make_client()
            async with client:
                with pytest.raises(MfaRequired):
                    await client.login()
                code = await client.start_mfa(_email_factor())

                with pytest.raises(AuthenticationError):
                    await client.try_finish_auth(code, otp="000000")


class TestStartMfaRateLimit:
    async def test_start_mfa_raises_rate_limit(self) -> None:
        with aioresponses() as m:
            _stub_until_mfa(m)
            m.post(
                f"{OCAPI}/api/startAuth",
                payload={
                    "data": {},
                    "meta": {"code": 429, "message": "Too many requests"},
                },
            )

            client = make_client()
            async with client:
                with pytest.raises(MfaRequired):
                    await client.login()
                with pytest.raises(RateLimitedError):
                    await client.start_mfa(_push_factor())


class TestAuthPayloadEncoding:
    """The /api/auth payload encodes password as base64."""

    async def test_auth_body_uses_b64_password(self) -> None:
        with aioresponses() as m:
            _stub_until_mfa(m)

            client = make_client()
            async with client:
                with pytest.raises(MfaRequired):
                    await client.login()

            auth_calls = [
                call
                for (method, url), calls in m.requests.items()
                if method == "POST" and "/api/auth" in str(url) and "/getFactorId" not in str(url)
                for call in calls
            ]
            assert auth_calls, "expected at least one /api/auth call"
            body = auth_calls[0].kwargs["json"]
            assert body["password"] == base64.b64encode(b"hunter2").decode()
