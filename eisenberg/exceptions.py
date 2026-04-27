"""Eisenberg exception hierarchy.

All exceptions derive from EisenbergError. Auth errors form a sub-tree
so callers can catch broadly or narrowly.
"""

from __future__ import annotations


class EisenbergError(Exception):
    """Base exception for all eisenberg errors."""


class AuthenticationError(EisenbergError):
    """Authentication failed (wrong credentials, expired token, etc.)."""


class PushApprovalRequired(AuthenticationError):  # noqa: N818
    """Browser trust expired — caller must explicitly trigger push.

    Marker exception. No push has been sent yet. Caller (config flow)
    must explicitly call start_push_login() to fire the push, then
    try_finish_auth() once the user approves on their phone.
    """

    def __init__(self) -> None:
        super().__init__("Push approval required")


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
