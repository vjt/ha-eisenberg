"""Eisenberg exception hierarchy.

All exceptions derive from EisenbergError. Auth errors form a sub-tree
so callers can catch broadly or narrowly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SecondFactor


class EisenbergError(Exception):
    """Base exception for all eisenberg errors."""


class AuthenticationError(EisenbergError):
    """Authentication failed (wrong credentials, expired token, etc.)."""


class MfaRequired(AuthenticationError):  # noqa: N818
    """Browser trust expired — caller must drive MFA.

    Carries the list of factors Arlo offered for this account so the
    caller (config flow) can let the user pick one. No challenge has
    been fired yet — call start_mfa(factor) to do that, then
    try_finish_auth() with the OTP (or empty for PUSH polling).
    """

    def __init__(self, factors: list[SecondFactor]) -> None:
        self.factors = factors
        super().__init__(f"MFA required: {len(factors)} factor(s) available")


class SessionExpiredError(AuthenticationError):
    """Token expired, re-auth needed."""


class RateLimitedError(EisenbergError):
    """Arlo is rate-limiting requests. Back off and retry later."""


class APIError(EisenbergError):
    """Arlo API returned an error response."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Arlo API error {code}: {message}")


class MQTTConnectionError(EisenbergError):
    """Failed to connect or maintain MQTT WebSocket connection."""
