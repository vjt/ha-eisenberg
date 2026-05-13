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
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from types import TracebackType

from aiohttp import ClientSession, CookieJar

from .exceptions import (
    APIError,
    AuthenticationError,
    MfaRequired,
    RateLimitedError,
    SessionExpiredError,
)
from .models import (
    ActiveModeState,
    DeviceInfo,
    FactorType,
    LocationInfo,
    SecondFactor,
    StreamResponse,
)

_LOGGER = logging.getLogger(__name__)

OCAPI_BASE = "https://ocapi-app.arlo.com"
MYAPI_BASE = "https://myapi.arlo.com"

# Arlo error codes that mean "your token is bad — log in again". Other codes
# are operational (rate-limit, mode-conflict, etc.) and not auth-related.
_INVALID_TOKEN_CODES: frozenset[str] = frozenset({"2015"})


def _raise_for_arlo_error(body: dict[str, Any], op: str) -> None:
    """Raise the right exception for a non-success Arlo REST response.

    Distinguishes Arlo's "Invalid Token" error (code 2015) from generic
    operational failures so callers can relogin-and-retry instead of
    bubbling a generic APIError up to the UI as a stack trace.
    """
    raw: Any = body.get("data")
    data: dict[str, Any] = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    err_code = str(data.get("error", "unknown"))
    message = str(data.get("message") or data.get("reason") or "")
    if err_code in _INVALID_TOKEN_CODES:
        raise SessionExpiredError(f"Arlo rejected token during {op}: {message or 'Invalid Token'}")
    raise APIError(code=err_code, message=f"{op} failed: {body}")


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
        """Silent login. Sets self.token, self.user_id, self.mqtt_url.

        NEVER fires a push or otherwise contacts the user. If the browser
        trust cookie is missing/expired, calls /api/getFactors to discover
        the account's available MFA factors and raises MfaRequired with
        them. The caller (config flow) lets the user pick a factor, then
        calls start_mfa(factor) to fire it.

        Raises AuthenticationError on bad credentials.
        Raises RateLimitedError if Arlo is rate-limiting requests.
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

        if body["meta"].get("message") == "Too many requests":
            raise RateLimitedError(
                "Arlo is rate-limiting requests. Wait a few hours and try again."
            )
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

        # Browser not trusted — discover factors (no side effect) and let
        # the caller drive the picker.
        _LOGGER.info("Browser not trusted — discovering MFA factors")
        self.token = token
        factors = await self._get_factors(token)
        raise MfaRequired(factors=factors)

    async def _get_factors(self, token: str) -> list[SecondFactor]:
        """GET /api/getFactors — returns factor list without firing MFA."""
        async with self.session.get(
            f"{OCAPI_BASE}/api/getFactors?data={int(time.time())}",
            headers=self._ocapi_headers(token),
        ) as resp:
            body = await resp.json()

        if body["meta"].get("message") == "Too many requests":
            raise RateLimitedError(
                "Arlo is rate-limiting requests. Wait a few hours and try again."
            )
        if body["meta"]["code"] != 200:
            raise AuthenticationError(f"getFactors failed: {body['meta'].get('error')}")

        items = body["data"].get("items", [])
        return [SecondFactor.model_validate(item) for item in items]

    async def start_mfa(self, factor: SecondFactor) -> str:
        """Fire the MFA challenge for `factor`. Returns factorAuthCode.

        PUSH: sends a notification to the chosen device; finish_auth() then
        polls /api/finishAuth (no otp) until the user taps approve.

        EMAIL/SMS: Arlo delivers a one-time code to the chosen factor; the
        caller must collect the code from the user and pass it to
        try_finish_auth(code, otp=<code>).

        Requires self.token + self.user_id from a prior login().
        """
        if self.token is None or self.user_id is None:
            raise AuthenticationError("start_mfa called before login()")

        # PingOne PUSH wants factorType empty; EMAIL/SMS want factorType "BROWSER"
        # (matches pyaarlo's working flow against the same backend).
        request_factor_type = "" if factor.factor_type == FactorType.PUSH else "BROWSER"

        _LOGGER.info(
            "Firing MFA: factor_type=%s display=%s",
            factor.factor_type,
            factor.display_name,
        )
        async with self.session.post(
            f"{OCAPI_BASE}/api/startAuth",
            headers=self._ocapi_headers(self.token),
            json={
                "factorId": factor.factor_id,
                "factorType": request_factor_type,
                "userId": self.user_id,
            },
        ) as resp:
            body = await resp.json()

        if body["meta"].get("message") == "Too many requests":
            raise RateLimitedError(
                "Arlo is rate-limiting requests. Wait a few hours and try again."
            )
        if body["meta"]["code"] != 200:
            raise AuthenticationError(f"startAuth failed: {body['meta'].get('error')}")

        return body["data"]["factorAuthCode"]

    async def try_finish_auth(
        self,
        factor_auth_code: str,
        otp: str | None = None,
    ) -> bool:
        """Single finishAuth call. Returns True if approved, False if pending.

        For PUSH: call with otp=None. Returns False until the user taps
        approve on the phone, then True. Each call increments Arlo's
        rate-limit counter — never call in a loop. The UI drives retries.

        For EMAIL/SMS: call with otp=<code-from-user>. Returns True on
        success, raises AuthenticationError on a bad code.

        Raises RateLimitedError on "Too many requests".
        """
        payload: dict[str, Any] = {
            "factorAuthCode": factor_auth_code,
            "isBrowserTrusted": True,
        }
        if otp is not None:
            payload["otp"] = otp

        async with self.session.post(
            f"{OCAPI_BASE}/api/finishAuth",
            headers=self._ocapi_headers(self.token),
            json=payload,
        ) as resp:
            body = await resp.json()

        meta = body["meta"]
        msg = meta.get("message", "")
        _LOGGER.info("finishAuth: code=%s msg=%s", meta["code"], msg)

        if msg == "Too many requests":
            raise RateLimitedError(
                "Arlo is rate-limiting requests. Wait a few hours and try again."
            )

        if meta["code"] == 200 and body["data"].get("authCompleted"):
            finish_data = body["data"]
            self.token = finish_data["token"]
            self._token_issued_at = time.monotonic()

            browser_auth_code = finish_data.get("browserAuthCode")
            if browser_auth_code:
                await self._pair_browser(browser_auth_code)

            await self._establish_session()
            return True

        if msg == "Authentication is not finished yet":
            return False

        raise AuthenticationError(f"finishAuth failed: {msg or meta.get('code')}")

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
            _raise_for_arlo_error(body, "establish_session")

        self.mqtt_url = body["data"].get("mqttUrl", "")

    def token_needs_refresh(self) -> bool:
        """Check if token is close to expiry.

        Arlo tokens live ~2 h. The HA coordinator polls this every 30 min,
        so refreshing at 60 min gives 30 min of headroom against the
        worst-case alignment (login fires just after a poll tick → next
        check at +30, refresh at +60-90, token still has ≥30 min left).
        """
        if self.token is None:
            return True
        elapsed = time.monotonic() - self._token_issued_at
        return elapsed > 3600  # 60 minutes

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
            _raise_for_arlo_error(body, "get_devices")

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
            _raise_for_arlo_error(body, "request_snapshot")

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
            _raise_for_arlo_error(body, "start_stream")

        return StreamResponse.model_validate(body["data"])

    def _v3_mode_headers(self, token: str) -> dict[str, str]:
        """Headers for the v3 location-based automation endpoints.

        pyaarlo adds x-forwarded-user / x-user-device-id so the server
        accepts the request as coming from this user; without them the
        endpoint returns 403.
        """
        if self.user_id is None:
            raise RuntimeError("Not authenticated")
        headers = self._myapi_headers(token)
        headers["x-forwarded-user"] = self.user_id
        headers["x-user-device-id"] = self.user_id
        return headers

    async def get_locations(self) -> list[LocationInfo]:
        """List the user's owned locations.

        Modes are scoped to a location in the v3 automation API. Most users
        have a single location. We pick the first one as the default.
        """
        if self.token is None or self.user_id is None:
            raise RuntimeError("Not authenticated")

        async with self.session.get(
            f"{MYAPI_BASE}/hmsdevicemanagement/users/{self.user_id}/locations",
            headers=self._myapi_headers(self.token),
        ) as resp:
            body = await resp.json()

        _LOGGER.debug("get_locations response: %s", body)

        try:
            data: Any = body["data"]
        except (KeyError, TypeError):
            return []

        raw: Any = None
        for key in ("userLocations", "ownedLocations", "locations"):
            try:
                raw = data[key]
            except (KeyError, TypeError):
                continue
            break
        if not isinstance(raw, list):
            return []

        return [LocationInfo.model_validate(item) for item in cast("list[Any]", raw)]

    async def get_active_mode(self, location_id: str) -> ActiveModeState:
        """GET the current active mode + revision for a location."""
        if self.token is None:
            raise RuntimeError("Not authenticated")

        async with self.session.get(
            f"{MYAPI_BASE}/hmsweb/automation/v3/activeMode",
            headers=self._v3_mode_headers(self.token),
            params={"locationId": location_id},
        ) as resp:
            body = await resp.json()

        _LOGGER.debug("get_active_mode response: %s", body)

        try:
            data: Any = body["data"]
        except (KeyError, TypeError):
            data = body
        return ActiveModeState.model_validate(data)

    async def set_active_mode(self, location_id: str, mode: str, revision: int) -> ActiveModeState:
        """Set the active mode for a location. Returns the new revision.

        Mirrors pyaarlo's set_mode: PUT activeMode?locationId=...&revision=N
        with body {"mode": "<name>"}. The server returns a fresh revision
        which the caller must store for the next call.
        """
        if self.token is None:
            raise RuntimeError("Not authenticated")

        async with self.session.put(
            f"{MYAPI_BASE}/hmsweb/automation/v3/activeMode",
            headers=self._v3_mode_headers(self.token),
            params={"locationId": location_id, "revision": str(revision)},
            json={"mode": mode},
        ) as resp:
            body = await resp.json()

        _LOGGER.debug("set_active_mode response: %s", body)

        try:
            success = body["success"]
        except (KeyError, TypeError):
            success = True
        if success is False:
            _raise_for_arlo_error(body, "set_active_mode")

        try:
            data: Any = body["data"]
        except (KeyError, TypeError):
            data = body
        return ActiveModeState.model_validate(data)

    async def set_spotlight(
        self,
        device_id: str,
        *,
        on: bool,
        intensity: int | None = None,
    ) -> None:
        """Turn the camera spotlight on/off and optionally set brightness.

        intensity is on Arlo's 0-100 scale. Only sent when provided so
        on/off toggles don't reset whatever brightness the user picked
        last via the app.
        """
        if self.token is None:
            raise RuntimeError("Not authenticated")

        spotlight: dict[str, Any] = {"enabled": on}
        if intensity is not None:
            spotlight["intensity"] = intensity

        async with self.session.post(
            f"{MYAPI_BASE}/hmsweb/users/devices/notify/{device_id}",
            headers=self._myapi_headers(self.token),
            json={
                "from": f"{self.user_id}_web",
                "to": device_id,
                "action": "set",
                "resource": f"cameras/{device_id}",
                "publishResponse": True,
                "transId": f"web!spotlight!{int(time.time())}",
                "properties": {"spotlight": spotlight},
            },
        ) as resp:
            body = await resp.json()

        if not body.get("success"):
            _raise_for_arlo_error(body, "set_spotlight")

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
            _raise_for_arlo_error(body, "set_siren")
