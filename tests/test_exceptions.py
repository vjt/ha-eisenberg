"""Tests for eisenberg exceptions hierarchy."""

from eisenberg.exceptions import (
    APIError,
    AuthenticationError,
    EisenbergError,
    MQTTConnectionError,
    PushApprovalRequired,
    SessionExpiredError,
)


def test_base_exception_is_exception() -> None:
    assert issubclass(EisenbergError, Exception)


def test_authentication_error_has_message() -> None:
    err = AuthenticationError("bad creds")
    assert str(err) == "bad creds"
    assert isinstance(err, EisenbergError)


def test_push_approval_required_carries_factors() -> None:
    factors = [{"factorType": "PUSH", "displayName": "Phone"}]
    err = PushApprovalRequired(
        factor_auth_code="abc123",
        factors=factors,
    )
    assert err.factor_auth_code == "abc123"
    assert err.factors == factors
    assert isinstance(err, AuthenticationError)


def test_session_expired_is_auth_error() -> None:
    assert issubclass(SessionExpiredError, AuthenticationError)


def test_api_error_has_code_and_message() -> None:
    err = APIError(code="2001", message="Invalid content")
    assert err.code == "2001"
    assert err.message == "Invalid content"
    assert "2001" in str(err)
    assert isinstance(err, EisenbergError)


def test_mqtt_connection_error() -> None:
    assert issubclass(MQTTConnectionError, EisenbergError)
