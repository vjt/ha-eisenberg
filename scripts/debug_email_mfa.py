"""Debug email-factor MFA flow end-to-end.

Two-step because we can't read stdin interactively:

    # Step 1: list factors
    python scripts/debug_email_mfa.py list EMAIL PASSWORD

    # Step 2: fire chosen factor (sends email)
    python scripts/debug_email_mfa.py fire EMAIL PASSWORD FACTOR_ID

    # Step 3: read OTP from inbox, finish
    python scripts/debug_email_mfa.py finish EMAIL PASSWORD FACTOR_AUTH_CODE OTP

Steps 2 and 3 re-do the /api/auth call so each invocation has a fresh token
(Arlo tokens are short-lived; multi-step CLI use across human latency is fine
because /api/auth + getFactorId doesn't trigger MFA on its own).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
import uuid

import aiohttp

OCAPI_BASE = "https://ocapi-app.arlo.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
_LOG = logging.getLogger("arlo_email_debug")


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


def _headers(device_id: str, token: str | None = None) -> dict[str, str]:
    h = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Source": "arloCamWeb",
        "Auth-Version": "2",
        "X-User-Device-Id": device_id,
        "X-User-Device-Type": "BROWSER",
        "X-User-Device-Automation-Name": base64.b64encode(b"BROWSER").decode(),
        "X-Service-Version": "v3",
        "Origin": "https://my.arlo.com",
        "Referer": "https://my.arlo.com/",
        "User-Agent": _BROWSER_UA,
    }
    if token:
        h["Authorization"] = base64.b64encode(token.encode()).decode()
    return h


async def _auth(
    session: aiohttp.ClientSession, email: str, password: str, device_id: str
) -> tuple[str, str]:
    """Run /api/auth. Returns (token, user_id)."""
    pwd_b64 = base64.b64encode(password.encode()).decode()
    async with session.post(
        f"{OCAPI_BASE}/api/auth",
        headers=_headers(device_id),
        json={
            "email": email,
            "password": pwd_b64,
            "language": "en",
            "EnvSource": "prod",
        },
    ) as resp:
        body = await resp.json()
    if body["meta"]["code"] != 200:
        raise RuntimeError(f"/api/auth failed: {body}")
    return body["data"]["token"], body["data"]["userId"]


async def cmd_list(email: str, password: str) -> None:
    """Discover factors without firing any MFA."""
    device_id = f"eisenberg-debug-{uuid.uuid4()}"
    _LOG.info("device_id=%s", device_id)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        token, user_id = await _auth(s, email, password, device_id)
        _LOG.info("authed: user_id=%s", user_id)

        # Try the same getFactors endpoint pyaarlo uses
        url = f"{OCAPI_BASE}/api/getFactors?data={int(asyncio.get_event_loop().time())}"
        async with s.get(url, headers=_headers(device_id, token)) as resp:
            text = await resp.text()
        _LOG.info("GET /api/getFactors -> %s", resp.status)
        _LOG.info("body: %s", text)


async def cmd_fire(email: str, password: str, factor_id: str) -> None:
    """Fire startAuth for given factor_id (EMAIL or SMS factor)."""
    device_id = f"eisenberg-debug-{uuid.uuid4()}"
    _LOG.info("device_id=%s", device_id)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        token, user_id = await _auth(s, email, password, device_id)
        _LOG.info("authed")

        payload = {
            "factorId": factor_id,
            "factorType": "BROWSER",
            "userId": user_id,
        }
        _LOG.info("startAuth payload: %s", payload)
        async with s.post(
            f"{OCAPI_BASE}/api/startAuth",
            headers=_headers(device_id, token),
            json=payload,
        ) as resp:
            text = await resp.text()
        _LOG.info("POST /api/startAuth -> %s", resp.status)
        _LOG.info("body: %s", text)


async def cmd_finish(
    email: str, password: str, factor_auth_code: str, otp: str
) -> None:
    """Submit OTP via /api/finishAuth."""
    device_id = f"eisenberg-debug-{uuid.uuid4()}"
    _LOG.info("device_id=%s", device_id)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        token, _ = await _auth(s, email, password, device_id)
        _LOG.info("authed")

        payload = {
            "factorAuthCode": factor_auth_code,
            "otp": otp,
            "isBrowserTrusted": True,
        }
        _LOG.info("finishAuth payload: factorAuthCode=%s..., otp=%s", factor_auth_code[:20], otp)
        async with s.post(
            f"{OCAPI_BASE}/api/finishAuth",
            headers=_headers(device_id, token),
            json=payload,
        ) as resp:
            text = await resp.text()
        _LOG.info("POST /api/finishAuth -> %s", resp.status)
        _LOG.info("body: %s", text)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "list" and len(sys.argv) == 4:
        asyncio.run(cmd_list(sys.argv[2], sys.argv[3]))
    elif cmd == "fire" and len(sys.argv) == 5:
        asyncio.run(cmd_fire(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif cmd == "finish" and len(sys.argv) == 6:
        asyncio.run(cmd_finish(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]))
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
