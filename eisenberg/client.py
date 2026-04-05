"""Arlo API client.

Typed async client for the Arlo REST API. Handles authentication
(both first-time push and trusted browser flows), session management,
and REST commands. Takes an aiohttp ClientSession via constructor
injection, or creates its own.

Cookie persistence for the browser trust cookie is handled externally
(by the HA integration config entry).
"""

from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import TracebackType

from aiohttp import ClientSession, CookieJar

from .exceptions import (
    APIError,
    AuthenticationError,
    PushApprovalRequired,
)
from .models import DeviceInfo, StreamResponse

_LOGGER = logging.getLogger(__name__)

OCAPI_BASE = "https://ocapi-app.arlo.com"
MYAPI_BASE = "https://myapi.arlo.com"

# Mobile UA to get RTSP URLs from startStream (not DASH)
_MOBILE_UA = "Arlo/4.0 (iPhone; iOS 18.0)"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


class EisenbergClient:
    """Async client for the Arlo camera API."""

    def __init__(
        self,
        email: str,
        password: str,
        device_id: str,
        cookie_jar: CookieJar | None = None,
        http_session: ClientSession | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._device_id = device_id
        self._cookie_jar = cookie_jar  # lazily created in __aenter__ if None
        self._external_session = http_session is not None
        self._session = http_session
        self._owns_session = False

        self.token: str | None = None
        self.user_id: str | None = None
        self.mqtt_url: str | None = None
        self._x_cloud_id: str | None = None
        self._token_issued_at: float = 0

    def set_http_session(self, session: ClientSession) -> None:
        """Inject an external HTTP session (coordinator uses this)."""
        self._session = session
        self._owns_session = False

    async def __aenter__(self) -> EisenbergClient:
        if self._session is None:
            if self._cookie_jar is None:
                self._cookie_jar = CookieJar(unsafe=True)
            self._session = ClientSession(cookie_jar=self._cookie_jar)
            self._owns_session = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("Client not initialized. Use async with.")
        return self._session

    @property
    def x_cloud_id(self) -> str:
        if self._x_cloud_id is None:
            raise RuntimeError("x_cloud_id not set. Call get_devices() first.")
        return self._x_cloud_id

    def _ocapi_headers(self, token: str | None = None) -> dict[str, str]:
        """Headers for ocapi-app.arlo.com requests."""
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Source": "arloCamWeb",
            "Auth-Version": "2",
            "X-User-Device-Id": self._device_id,
            "X-User-Device-Type": "BROWSER",
            "X-User-Device-Automation-Name": base64.b64encode(b"BROWSER").decode(),
            "X-Service-Version": "v3",
            "Origin": "https://my.arlo.com",
            "Referer": "https://my.arlo.com/",
            "User-Agent": _BROWSER_UA,
        }
        if token:
            headers["Authorization"] = base64.b64encode(token.encode()).decode()
        return headers

    def _myapi_headers(self, token: str) -> dict[str, str]:
        """Headers for myapi.arlo.com — raw token, not base64."""
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Authorization": token,
            "Auth-Version": "2",
            "xCloudId": self._x_cloud_id or "",
            "Origin": "https://my.arlo.com",
            "Referer": "https://my.arlo.com/",
            "User-Agent": _BROWSER_UA,
        }

    def _myapi_headers_mobile(self, token: str) -> dict[str, str]:
        """Headers for myapi.arlo.com with mobile UA (for RTSP streams)."""
        headers = self._myapi_headers(token)
        headers["User-Agent"] = _MOBILE_UA
        headers["x-user-device-type"] = "PHONE"
        return headers

    async def login(self) -> None:
        """Full auth flow. Sets self.token, self.user_id, self.mqtt_url.

        Raises PushApprovalRequired if this is the first login and the
        browser trust cookie is missing. The caller (config flow) should
        show a UI step asking the user to approve the push, then call
        complete_push_approval().

        Raises AuthenticationError on bad credentials.
        """
        password_b64 = base64.b64encode(self._password.encode()).decode()

        # Step 1: Initial auth
        async with self.session.post(
            f"{OCAPI_BASE}/api/auth",
            headers=self._ocapi_headers(),
            json={
                "email": self._email,
                "password": password_b64,
                "language": "en",
                "EnvSource": "prod",
            },
        ) as resp:
            body = await resp.json()

        if body["meta"]["code"] != 200:
            raise AuthenticationError(f"Auth failed: {body['meta'].get('error', 'unknown')}")

        auth_data = body["data"]
        token = auth_data["token"]
        self.user_id = auth_data["userId"]

        if auth_data.get("authCompleted"):
            self.token = token
            self._token_issued_at = time.monotonic()
            await self._establish_session()
            return

        # Step 2: Check browser trust
        async with self.session.post(
            f"{OCAPI_BASE}/api/getFactorId",
            headers=self._ocapi_headers(token),
            json={
                "factorType": "BROWSER",
                "factorData": "",
                "userId": self.user_id,
            },
        ) as resp:
            body = await resp.json()

        _LOGGER.debug("getFactorId response: code=%s", body["meta"]["code"])
        if body["meta"]["code"] == 200:
            # Browser trusted — instant auth with factorId
            factor_id = body["data"]["factorId"]
            async with self.session.post(
                f"{OCAPI_BASE}/api/startAuth",
                headers=self._ocapi_headers(token),
                json={
                    "factorId": factor_id,
                    "factorType": "BROWSER",
                    "userId": self.user_id,
                },
            ) as resp:
                body = await resp.json()

            if body["meta"]["code"] != 200:
                raise AuthenticationError(f"Trusted startAuth failed: {body['meta'].get('error')}")

            start_data = body["data"]
            if not start_data.get("authCompleted"):
                raise AuthenticationError("Trusted browser auth did not auto-complete")

            self.token = start_data["accessToken"]["token"]
            self._token_issued_at = time.monotonic()
            await self._establish_session()
            return

        # Browser not trusted — first-time flow, need push approval
        _LOGGER.info("Browser not trusted, initiating push approval")
        async with self.session.post(
            f"{OCAPI_BASE}/api/startAuth",
            headers=self._ocapi_headers(token),
            json={"factorType": "", "userId": self.user_id},
        ) as resp:
            body = await resp.json()

        if body["meta"]["code"] != 200:
            raise AuthenticationError(f"startAuth failed: {body['meta'].get('error')}")

        start_data = body["data"]
        # Store token for use in complete_push_approval
        self.token = token
        raise PushApprovalRequired(
            factor_auth_code=start_data["factorAuthCode"],
            factors=start_data["factors"],
        )

    async def complete_push_approval(
        self,
        factor_auth_code: str,
        timeout: int = 120,
        poll_interval: int = 3,
    ) -> None:
        """Poll finishAuth until user approves push, then establish trust.

        Called after login() raises PushApprovalRequired.
        """
        import asyncio

        _LOGGER.debug("Polling finishAuth for push approval")
        elapsed = 0
        while elapsed < timeout:
            async with self.session.post(
                f"{OCAPI_BASE}/api/finishAuth",
                headers=self._ocapi_headers(self.token),
                json={
                    "factorAuthCode": factor_auth_code,
                    "isBrowserTrusted": True,
                },
            ) as resp:
                body = await resp.json()

            _LOGGER.debug("finishAuth: code=%s", body["meta"]["code"])
            if body["meta"]["code"] == 200 and body["data"].get("authCompleted"):
                finish_data = body["data"]
                self.token = finish_data["token"]
                self._token_issued_at = time.monotonic()

                # Trust this browser
                browser_auth_code = finish_data.get("browserAuthCode")
                if browser_auth_code:
                    await self._pair_browser(browser_auth_code)

                await self._establish_session()
                return

            elapsed += poll_interval
            await asyncio.sleep(poll_interval)

        raise AuthenticationError(f"Push approval not received within {timeout}s")

    async def _pair_browser(self, browser_auth_code: str) -> None:
        """Register this browser as trusted (sets 14-day cookie)."""
        async with self.session.post(
            f"{OCAPI_BASE}/api/startPairingFactor",
            headers=self._ocapi_headers(self.token),
            json={
                "factorType": "BROWSER",
                "factorData": "",
                "factorAuthCode": browser_auth_code,
            },
        ) as resp:
            body = await resp.json()

        if body["meta"]["code"] != 200:
            _LOGGER.warning("Failed to pair browser: %s", body)

    async def _establish_session(self) -> None:
        """Get session info (MQTT URL) from myapi."""
        if self.token is None:
            raise RuntimeError("Cannot establish session without token")

        async with self.session.get(
            f"{MYAPI_BASE}/hmsweb/users/session/v3",
            headers=self._myapi_headers(self.token),
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Session establishment failed",
            )

        self.mqtt_url = body["data"].get("mqttUrl", "")

    def token_needs_refresh(self) -> bool:
        """Check if token is close to expiry (~2hr lifetime, refresh at 90min)."""
        if self.token is None:
            return True
        elapsed = time.monotonic() - self._token_issued_at
        return elapsed > 5400  # 90 minutes

    async def get_devices(self) -> list[DeviceInfo]:
        """Fetch all devices. Sets x_cloud_id from first camera found."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        async with self.session.get(
            f"{MYAPI_BASE}/hmsweb/v2/users/devices",
            headers=self._myapi_headers(self.token),
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Failed to list devices",
            )

        devices = [DeviceInfo.model_validate(d) for d in body["data"]]

        # Set xCloudId from first device
        if devices and self._x_cloud_id is None:
            self._x_cloud_id = devices[0].x_cloud_id

        return devices

    async def request_snapshot(self, device_id: str) -> None:
        """Request a full-frame snapshot. Response comes via MQTT."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        async with self.session.post(
            f"{MYAPI_BASE}/hmsweb/users/devices/notify/{device_id}",
            headers=self._myapi_headers(self.token),
            json={
                "from": f"{self.user_id}_web",
                "to": device_id,
                "action": "set",
                "resource": f"cameras/{device_id}",
                "publishResponse": True,
                "properties": {"activityState": "fullFrameSnapshot"},
                "transId": f"web!snapshot!{int(time.time())}",
            },
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Snapshot request failed",
            )

    async def start_stream(self, device_id: str) -> StreamResponse:
        """Start a live stream. Returns RTSP URL (uses mobile UA)."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        async with self.session.post(
            f"{MYAPI_BASE}/hmsweb/users/devices/startStream",
            headers=self._myapi_headers_mobile(self.token),
            json={
                "from": f"{self.user_id}_web",
                "to": device_id,
                "action": "set",
                "resource": f"cameras/{device_id}",
                "publishResponse": True,
                "transId": f"web!stream!{int(time.time())}",
                "properties": {
                    "activityState": "startUserStream",
                    "cameraId": device_id,
                },
            },
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Stream start failed",
            )

        return StreamResponse.model_validate(body["data"])

    async def set_siren(self, device_id: str, *, on: bool) -> None:
        """Turn siren on or off."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        properties: dict[str, Any] = {
            "sirenState": "on" if on else "off",
        }
        if on:
            properties["duration"] = 180
            properties["volume"] = 8
            properties["pattern"] = "alarm"

        async with self.session.post(
            f"{MYAPI_BASE}/hmsweb/users/devices/notify/{device_id}",
            headers=self._myapi_headers(self.token),
            json={
                "from": f"{self.user_id}_web",
                "to": device_id,
                "action": "set",
                "resource": f"siren/{device_id}",
                "publishResponse": True,
                "transId": f"web!siren!{int(time.time())}",
                "properties": properties,
            },
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            raise APIError(
                code=body.get("data", {}).get("error", "unknown"),
                message="Siren command failed",
            )
