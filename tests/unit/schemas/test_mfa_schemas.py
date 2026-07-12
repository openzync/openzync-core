"""Unit tests for MFA-related Pydantic schemas — serialization and validation.

Covers LoginResponse (dual-mode), MfaVerifyRequest, MfaEnableRequest,
and MfaDisableRequest.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.auth import (
    LoginResponse,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaVerifyRequest,
)


@pytest.mark.unit
class TestLoginResponseSchema:
    """LoginResponse serialization — MFA path vs. token path."""

    def test_mfa_path_serialization(self) -> None:
        """LoginResponse with MFA challenge has requires_mfa=True and session_token."""
        payload = LoginResponse(
            requires_mfa=True,
            mfa_session_token="mfa-session-xyz",
        )

        # These should be None when MFA is required
        assert payload.access_token is None
        assert payload.refresh_token is None
        assert payload.expires_in is None
        assert payload.token_type is None

        assert payload.requires_mfa is True
        assert payload.mfa_session_token == "mfa-session-xyz"

        # Round-trip through dict to verify serialization
        data = payload.model_dump()
        assert data["requires_mfa"] is True
        assert data["mfa_session_token"] == "mfa-session-xyz"
        assert data["access_token"] is None

    def test_token_path_serialization(self) -> None:
        """LoginResponse with tokens has requires_mfa=False and token fields set."""
        payload = LoginResponse(
            access_token="eyJ...",
            refresh_token="abc123",
            expires_in=1800,
            token_type="Bearer",
            requires_mfa=False,
        )

        assert payload.access_token == "eyJ..."
        assert payload.refresh_token == "abc123"
        assert payload.expires_in == 1800
        assert payload.token_type == "Bearer"
        assert payload.requires_mfa is False
        assert payload.mfa_session_token is None

        # Round-trip through dict
        data = payload.model_dump()
        assert data["access_token"] == "eyJ..."
        assert data["refresh_token"] == "abc123"
        assert data["expires_in"] == 1800
        assert data["token_type"] == "Bearer"
        assert data["requires_mfa"] is False
        assert data["mfa_session_token"] is None

    def test_defaults(self) -> None:
        """LoginResponse defaults require neither tokens nor MFA."""
        payload = LoginResponse()

        assert payload.requires_mfa is False
        assert payload.access_token is None
        assert payload.refresh_token is None
        assert payload.mfa_session_token is None

    def test_requires_mfa_without_session_token_is_valid(self) -> None:
        """requires_mfa=True without a session token is schema-valid
        (business logic enforces presence at the service layer)."""
        payload = LoginResponse(requires_mfa=True)
        assert payload.requires_mfa is True
        assert payload.mfa_session_token is None


@pytest.mark.unit
class TestMfaVerifyRequestSchema:
    """MfaVerifyRequest validation."""

    def test_valid_data(self) -> None:
        """MfaVerifyRequest accepts valid email, OTP, and session token."""
        payload = MfaVerifyRequest(
            email="user@example.com",
            otp="483926",
            mfa_session_token="session-abc",
        )

        assert payload.email == "user@example.com"
        assert payload.otp == "483926"
        assert payload.mfa_session_token == "session-abc"

    def test_otp_too_short_raises_validation_error(self) -> None:
        """OTP with fewer than 4 characters fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            MfaVerifyRequest(
                email="user@example.com",
                otp="123",  # too short — min_length=4
                mfa_session_token="session-abc",
            )

        errors = exc_info.value.errors()
        assert any("otp" in e["loc"] for e in errors)

    def test_otp_exceeds_max_length_raises_validation_error(self) -> None:
        """OTP with more than 8 characters fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            MfaVerifyRequest(
                email="user@example.com",
                otp="123456789",  # too long — max_length=8
                mfa_session_token="session-abc",
            )

        errors = exc_info.value.errors()
        assert any("otp" in e["loc"] for e in errors)

    def test_invalid_email_raises_validation_error(self) -> None:
        """Non-email string fails email validation."""
        with pytest.raises(ValidationError) as exc_info:
            MfaVerifyRequest(
                email="not-an-email",
                otp="123456",
                mfa_session_token="session-abc",
            )

        errors = exc_info.value.errors()
        assert any("email" in e["loc"] for e in errors)

    def test_empty_session_token_raises_validation_error(self) -> None:
        """Empty mfa_session_token fails min_length validation."""
        with pytest.raises(ValidationError) as exc_info:
            MfaVerifyRequest(
                email="user@example.com",
                otp="123456",
                mfa_session_token="",  # too short — min_length=1
            )

        errors = exc_info.value.errors()
        assert any("mfa_session_token" in e["loc"] for e in errors)

    def test_otp_4_chars_is_valid(self) -> None:
        """OTP at the minimum length (4) is accepted."""
        payload = MfaVerifyRequest(
            email="user@example.com",
            otp="1234",
            mfa_session_token="session-abc",
        )
        assert payload.otp == "1234"

    def test_otp_8_chars_is_valid(self) -> None:
        """OTP at the maximum length (8) is accepted."""
        payload = MfaVerifyRequest(
            email="user@example.com",
            otp="12345678",
            mfa_session_token="session-abc",
        )
        assert payload.otp == "12345678"


@pytest.mark.unit
class TestMfaEnableRequestSchema:
    """MfaEnableRequest validation."""

    def test_valid_data(self) -> None:
        """MfaEnableRequest accepts a valid password."""
        payload = MfaEnableRequest(password="my-password")
        assert payload.password == "my-password"

    def test_empty_password_raises_validation_error(self) -> None:
        """Empty password fails min_length validation."""
        with pytest.raises(ValidationError) as exc_info:
            MfaEnableRequest(password="")

        errors = exc_info.value.errors()
        assert any("password" in e["loc"] for e in errors)


@pytest.mark.unit
class TestMfaDisableRequestSchema:
    """MfaDisableRequest validation."""

    def test_valid_data(self) -> None:
        """MfaDisableRequest accepts valid password and OTP."""
        payload = MfaDisableRequest(password="my-password", otp="654321")
        assert payload.password == "my-password"
        assert payload.otp == "654321"

    def test_empty_password_raises_validation_error(self) -> None:
        """Empty password fails min_length validation."""
        with pytest.raises(ValidationError) as exc_info:
            MfaDisableRequest(password="", otp="123456")

        errors = exc_info.value.errors()
        assert any("password" in e["loc"] for e in errors)

    def test_otp_too_short_raises_validation_error(self) -> None:
        """OTP below 4 characters fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            MfaDisableRequest(password="pass", otp="123")

        errors = exc_info.value.errors()
        assert any("otp" in e["loc"] for e in errors)

    def test_otp_too_long_raises_validation_error(self) -> None:
        """OTP above 8 characters fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            MfaDisableRequest(password="pass", otp="123456789")

        errors = exc_info.value.errors()
        assert any("otp" in e["loc"] for e in errors)
