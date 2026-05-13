"""Tests for eisenberg exceptions hierarchy."""

from eisenberg.exceptions import (
    APIError,
    AuthenticationError,
    EisenbergError,
    MfaRequired,
    MQTTConnectionError,
    SessionExpiredError,
)
from eisenberg.models import FactorType, SecondFactor


def test_base_exception_is_exception() -> None:
    assert issubclass(EisenbergError, Exception)


def test_authentication_error_has_message() -> None:
    err = AuthenticationError("bad creds")
    assert str(err) == "bad creds"
    assert isinstance(err, EisenbergError)


def test_mfa_required_carries_factors() -> None:
    factors = [
        SecondFactor.model_validate(
            {
                "factorId": "fid-push",
                "factorType": "PUSH",
                "displayName": "iPhone 16",
                "factorNickname": "iPhone 16",
                "factorRole": "PRIMARY",
            }
        ),
    ]
    err = MfaRequired(factors=factors)
    assert isinstance(err, AuthenticationError)
    assert err.factors == factors
    assert "1 factor" in str(err)


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


def test_factor_type_values() -> None:
    assert FactorType.PUSH.value == "PUSH"
    assert FactorType.EMAIL.value == "EMAIL"
    assert FactorType.SMS.value == "SMS"
