"""Pydantic schemas for dashboard authentication.

Covers signup, login, token refresh, and the token response format.
All auth schemas are request/response models — never stored or logged directly.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignupRequest(BaseModel):
    """Request body for ``POST /v1/auth/signup``.

    Creates a new organization with an admin dashboard user.
    """

    email: EmailStr = Field(
        ...,
        description="Email address for the dashboard admin user.",
        examples=["admin@acme.com"],
    )
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Password (min 8 chars, max 128).",
        examples=["secure-p@ssword-123"],
    )
    organization_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable organization name.",
        examples=["Acme Corp"],
    )

class SignupResponse(BaseModel):
    """Response body for ``POST /v1/auth/signup``.

    Instead of returning tokens directly (the user must first verify their
    email), signup returns a confirmation message.  The client should then
    call ``POST /v1/auth/verify-email`` with the OTP received via email.
    """

    message: str = Field(
        ...,
        description="Human-readable confirmation message.",
        examples=["Verification code sent to email"],
    )
    email: EmailStr = Field(
        ...,
        description="Email address the verification code was sent to.",
        examples=["admin@acme.com"],
    )


class VerifyEmailRequest(BaseModel):
    """Request body for ``POST /v1/auth/verify-email``.

    The OTP is a 6-digit code received via email.  On success, returns a
    ``TokenResponse`` so the user is immediately authenticated.
    """

    email: EmailStr = Field(
        ...,
        description="Email address the OTP was sent to.",
        examples=["admin@acme.com"],
    )
    otp: str = Field(
        ...,
        min_length=4,
        max_length=8,
        description="The one-time passcode received via email.",
        examples=["483926"],
    )


class LoginRequest(BaseModel):
    """Request body for ``POST /v1/auth/login``."""

    email: EmailStr = Field(
        ...,
        description="Email address of the dashboard user.",
        examples=["admin@acme.com"],
    )
    password: str = Field(
        ...,
        min_length=1,
        description="Dashboard user password.",
    )


class TokenResponse(BaseModel):
    """Response body for login and refresh endpoints.

    Attributes:
        access_token: Short-lived JWT for API authentication.
        refresh_token: Long-lived token for session renewal.
        expires_in: Access token TTL in seconds.
        token_type: Always ``"Bearer"``.
    """

    access_token: str = Field(
        ..., description="JWT access token (Bearer)."
    )
    refresh_token: str = Field(
        ..., description="Opaque refresh token for session renewal."
    )
    expires_in: int = Field(
        ..., description="Access token TTL in seconds.", examples=[1800]
    )
    token_type: str = Field(
        default="Bearer",
        description="Token type — always ``'Bearer'``.",
    )


class RefreshRequest(BaseModel):
    """Request body for ``POST /v1/auth/refresh``."""

    refresh_token: str = Field(
        ...,
        min_length=1,
        description="The refresh token obtained from login.",
    )


class DashboardUserResponse(BaseModel):
    """Public-facing dashboard user profile.

    Returned by user-info endpoints — never includes the password hash.
    """

    id: UUID = Field(..., description="User UUID.")
    email: str = Field(..., description="Email address.")
    name: str | None = Field(default=None, description="Display name.")
    role: str = Field(default="member", description="User role.")
    organization_id: UUID = Field(..., description="Owning organization ID.")
    is_email_verified: bool = Field(
        default=False,
        description="Whether the user's email has been verified.",
    )
    mfa_enabled: bool = Field(
        default=False,
        description="Whether MFA is enabled.",
    )

    model_config = ConfigDict(from_attributes=True)


class LoginResponse(BaseModel):
    """Unified response for ``POST /v1/auth/login``.

    When MFA is disabled, returns JWT tokens as normal.
    When MFA is enabled, returns ``requires_mfa=true`` with a
    ``mfa_session_token`` for the second step.
    """

    access_token: str | None = Field(default=None, description="JWT access token (null when MFA required).")
    refresh_token: str | None = Field(default=None, description="Opaque refresh token (null when MFA required).")
    expires_in: int | None = Field(default=None, description="Access token TTL in seconds.")
    token_type: str | None = Field(default=None, description="Token type — ``Bearer``.")
    requires_mfa: bool = Field(default=False, description="Whether MFA verification is required.")
    mfa_session_token: str | None = Field(default=None, description="Session token for MFA step 2 (null when MFA not required).")


class MfaVerifyRequest(BaseModel):
    """Request body for ``POST /v1/auth/mfa/verify`` — second step of MFA login."""

    email: EmailStr = Field(..., description="Email address.")
    otp: str = Field(..., min_length=4, max_length=8, description="The MFA one-time passcode.")
    mfa_session_token: str = Field(..., min_length=1, description="Session token from the login response.")


class MfaEnableRequest(BaseModel):
    """Request body for ``POST /v1/auth/mfa/enable``."""

    password: str = Field(..., min_length=1, description="Current password for re-authentication.")


class MfaDisableRequest(BaseModel):
    """Request body for ``POST /v1/auth/mfa/disable``."""

    password: str = Field(..., min_length=1, description="Current password for re-authentication.")
    otp: str = Field(..., min_length=4, max_length=8, description="MFA OTP for verification.")


class UpdateProfileRequest(BaseModel):
    """Request body for ``PATCH /v1/auth/me``.

    All fields are optional. Only provided fields are updated.
    To change the password, provide both ``current_password`` and
    ``new_password``.
    """

    name: str | None = Field(
        default=None,
        description="New display name. Set to ``null`` to clear.",
        max_length=512,
    )
    email: str | None = Field(
        default=None,
        description="New email address.",
        max_length=320,
    )
    current_password: str | None = Field(
        default=None,
        min_length=1,
        description="Current password — required when setting a new password.",
    )
    new_password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description="New password (min 8 chars). Requires ``current_password``.",
    )
