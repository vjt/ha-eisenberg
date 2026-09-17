"""Auth failures must reach the user, and must not be retried into a lockout.

Issue #35 (peteramelang, 0.4.3). Three findings, and the third turned out to
be why the first one happened at all.

1. An auth failure discovered while starting a stream never raised a reauth
   prompt. `call_with_session_retry`'s docstring claimed the exception would
   "bubble — the coordinator's update loop turns it into a HA reauth flow",
   but the caller here is `camera.stream_source`, not the update loop, and it
   catches everything and returns None. The entry kept reporting `loaded`, the
   user was never told, and **72 start_stream rejections were logged in about
   ten minutes** — each one spending another silent login, which is exactly
   what `_login_silent` warns can lock the account out.

2. `client.login()` raised `KeyError: 'meta'` on a response without that key,
   from inside its own rate-limit check. 0.4.2 (#32) put a typed error around
   bodies that are not JSON at all, and the reply on that issue said in as many
   words that `body["meta"]["code"]` on an unvalidated body was the remaining
   hole. It was left open, and this is it closing on a real install.

3. He measured a token refresh at 5,845s (97 min) when the documented worst
   case is 90, and guessed the schedule was relative to the previous tick
   rather than a fixed grid. It is worse than that: `async_set_updated_data`
   is documented as "reset refresh interval", and every MQTT push called it,
   so the 30-minute health check only ever fired after 30 minutes of total
   MQTT silence. On a busy account it is starved indefinitely — which starves
   the token refresh, and an expired token is precisely where finding 1 starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg import AuthenticationError, MfaRequired, RateLimitedError, SessionExpiredError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _Entry:
    def __init__(self) -> None:
        self.reauth_started = 0

    def async_start_reauth(self, hass: Any) -> None:
        self.reauth_started += 1


def _coordinator(login_error: BaseException | None = None) -> Any:
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord.hass = object()  # type: ignore[assignment]
    coord.entry = _Entry()  # type: ignore[assignment]
    coord.data = {}
    coord.logins = 0
    # __init__ is bypassed above; this is the one flag call_with_session_retry
    # reads before it does anything else.
    coord._auth_is_dead = False

    async def _login_silent() -> None:
        coord.logins += 1
        if login_error is not None:
            raise login_error

    coord._login_silent = _login_silent  # type: ignore[method-assign]
    return coord


def _op(results: list[str]) -> Callable[[], Awaitable[str]]:
    async def op() -> str:
        results.append("called")
        raise SessionExpiredError("Invalid Token")

    return op


class TestAuthFailureReachesTheUser:
    @pytest.mark.parametrize(
        "error",
        [
            AuthenticationError("Trusted startAuth failed: 9303"),
            MfaRequired(factors=[]),
            RateLimitedError("Too many requests"),
        ],
        ids=["startauth-9303", "mfa-required", "rate-limited"],
    )
    async def test_a_dead_session_starts_a_reauth_flow(self, error: BaseException) -> None:
        """Whatever the caller, the user has to be told the account needs them."""
        coord = _coordinator(login_error=error)
        with pytest.raises(ConfigEntryAuthFailed):
            await coord.call_with_session_retry("start_stream", _op([]))
        assert coord.entry.reauth_started == 1

    async def test_further_calls_fail_fast_without_spending_a_login(self) -> None:
        """72 rejections in ten minutes, each burning a login attempt, is how an
        account gets locked out. Once the session is known dead, stop asking."""
        coord = _coordinator(login_error=AuthenticationError("Trusted startAuth failed: 9303"))
        calls: list[str] = []

        for _ in range(20):
            with pytest.raises(ConfigEntryAuthFailed):
                await coord.call_with_session_retry("start_stream", _op(calls))

        assert coord.logins == 1, "only the first failure may spend a login attempt"
        assert coord.entry.reauth_started == 1, "and the user is told exactly once"

    async def test_a_successful_relogin_clears_the_block(self) -> None:
        """A transient rejection must not wedge the integration permanently."""
        coord = _coordinator()
        attempts: list[str] = []

        async def op() -> str:
            attempts.append("called")
            if len(attempts) == 1:
                raise SessionExpiredError("Invalid Token")
            return "ok"

        assert await coord.call_with_session_retry("start_stream", op) == "ok"
        assert coord.entry.reauth_started == 0
        assert await coord.call_with_session_retry("start_stream", op) == "ok"

    async def test_a_non_auth_failure_is_not_treated_as_a_dead_session(self) -> None:
        """A WAF block page says nothing about the credentials (#32), so it must
        not start a reauth the user cannot act on."""
        from eisenberg.exceptions import TransientAPIError

        coord = _coordinator(login_error=TransientAPIError("403 text/html, not JSON"))
        with pytest.raises(TransientAPIError):
            await coord.call_with_session_retry("start_stream", _op([]))
        assert coord.entry.reauth_started == 0
