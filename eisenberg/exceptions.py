"""Eisenberg exception hierarchy.

All exceptions derive from EisenbergError. Auth errors form a sub-tree
so callers can catch broadly or narrowly.
"""

from __future__ import annotations

from typing import Any


class EisenbergError(Exception):
    """Base exception for all eisenberg errors."""


class AuthenticationError(EisenbergError):
    """Authentication failed (wrong credentials, expired token, etc.)."""


class PushApprovalRequired(AuthenticationError):  # noqa: N818
    """2FA push approval needed. Carries factor info for the UI to display."""

    def __init__(
        self,
        factor_auth_code: str,
        factors: list[dict[str, Any]],
    ) -> None:
        self.factor_auth_code = factor_auth_code
        self.factors = factors
        super().__init__("Push approval required")


class SessionExpiredError(AuthenticationError):
    """Token expired, re-auth needed."""


class APIError(EisenbergError):
    """Arlo API returned an error response."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Arlo API error {code}: {message}")


class MQTTConnectionError(EisenbergError):
    """Failed to connect or maintain MQTT WebSocket connection."""
