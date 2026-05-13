"""Smoke-test the new multi-factor auth flow against live Arlo.

Two-step because we can't drive stdin interactively:

    # Step 1: fire EMAIL factor for the account
    python scripts/debug_login.py fire EMAIL PASSWORD

    # Step 2: with the OTP from inbox, finish auth
    python scripts/debug_login.py finish EMAIL PASSWORD FACTOR_AUTH_CODE OTP

Each invocation builds a fresh EisenbergClient; the factorAuthCode is
account-bound (not session-bound), confirmed in earlier debugging.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid

from eisenberg import EisenbergClient, FactorType, MfaRequired

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_LOG = logging.getLogger("arlo_smoke")


async def cmd_fire(email: str, password: str) -> None:
    """login() → MfaRequired → pick EMAIL factor → start_mfa()."""
    device_id = f"eisenberg-smoke-{uuid.uuid4()}"
    _LOG.info("device_id=%s", device_id)

    client = EisenbergClient(email=email, password=password, device_id=device_id)
    async with client:
        try:
            await client.login()
        except MfaRequired as err:
            _LOG.info("MfaRequired raised — %d factors", len(err.factors))
            for f in err.factors:
                _LOG.info(
                    "  factor: type=%s display=%s role=%s",
                    f.factor_type,
                    f.display_name,
                    f.factor_role,
                )
            email_factor = next(
                (f for f in err.factors if f.factor_type == FactorType.EMAIL),
                None,
            )
            if email_factor is None:
                _LOG.error("No EMAIL factor — cannot smoke-test this path")
                return
            _LOG.info("Firing EMAIL factor: %s", email_factor.display_name)
            code = await client.start_mfa(email_factor)
            _LOG.info("factorAuthCode = %s", code)
        else:
            _LOG.info("login() returned silently (trusted) — token=%s", client.token)


async def cmd_finish(email: str, password: str, factor_auth_code: str, otp: str) -> None:
    """try_finish_auth(code, otp=...) — completes EMAIL MFA."""
    device_id = f"eisenberg-smoke-{uuid.uuid4()}"
    _LOG.info("device_id=%s", device_id)

    client = EisenbergClient(email=email, password=password, device_id=device_id)
    async with client:
        try:
            await client.login()
        except MfaRequired:
            # Expected — discovery side-effect-free; token stashed for finishAuth.
            _LOG.info("login() raised MfaRequired (token stashed)")
        else:
            _LOG.info("login() returned silently — unexpected for smoke test")
            return

        approved = await client.try_finish_auth(factor_auth_code, otp=otp)
        _LOG.info("try_finish_auth returned %s", approved)
        if approved:
            _LOG.info("token=%s mqtt_url=%s", client.token, client.mqtt_url)
            devices = await client.get_devices()
            _LOG.info("Fetched %d devices", len(devices))


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write(__doc__ or "")
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "fire" and len(sys.argv) == 4:
        asyncio.run(cmd_fire(sys.argv[2], sys.argv[3]))
    elif cmd == "finish" and len(sys.argv) == 6:
        asyncio.run(cmd_finish(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]))
    else:
        sys.stderr.write(__doc__ or "")
        sys.exit(2)


if __name__ == "__main__":
    main()
