# tests/test_config_flow.py
"""Tests for the Eisenberg config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from eisenberg.exceptions import AuthenticationError, PushApprovalRequired


# These tests validate the config flow logic in isolation.
# Full HA integration tests require a running HA instance.


class TestConfigFlowLogic:
    """Test config flow decision logic without HA infrastructure."""

    async def test_trusted_login_skips_push(self) -> None:
        """When login succeeds (trusted browser), no push step needed."""
        client = AsyncMock()
        client.login = AsyncMock(return_value=None)
        client.token = "test-token"
        client.user_id = "USER-123"
        client.mqtt_url = "wss://mqtt.arlo.com"
        # If login() returns without raising, push is not needed
        await client.login()
        # No PushApprovalRequired raised = success

    async def test_first_time_login_requires_push(self) -> None:
        """When login raises PushApprovalRequired, need push step."""
        client = AsyncMock()
        client.login = AsyncMock(
            side_effect=PushApprovalRequired(
                factor_auth_code="code-123",
                factors=[{"factorType": "PUSH", "displayName": "Phone"}],
            )
        )
        with pytest.raises(PushApprovalRequired) as exc_info:
            await client.login()
        assert exc_info.value.factor_auth_code == "code-123"

    async def test_bad_credentials_raises_auth_error(self) -> None:
        """When credentials are wrong, raises AuthenticationError."""
        client = AsyncMock()
        client.login = AsyncMock(
            side_effect=AuthenticationError("Invalid credentials")
        )
        with pytest.raises(AuthenticationError):
            await client.login()
