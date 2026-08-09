"""Pydantic schemas for dashboard authentication.

Covers signup, login, token refresh, and the token response format.
All auth schemas are request/response models — never stored or logged directly.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


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


class JoinRequest(BaseModel):
    """Request body for ``POST /v1/auth/join``.

    Joins an **existing** organization via its join code.  Creates a
    member dashboard user (never an admin).
    """

    email: EmailStr = Field(
        ...,
        description="Email address for the new member user.",
        examples=["alice@acme.com"],
    )
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Password (min 8 chars, max 128).",
        examples=["secure-p@ssword-123"],
    )
    org_code: str = Field(
        ...,
        min_length=1,
        max_length=24,
        description="Organization join code (case-insensitive).",
        examples=["K7M2Q9X4"],
    )


class OrgCodeResponse(BaseModel):
    """Response body for org-code admin endpoints.

    Returned by ``GET /admin/org/org-code``,
    ``PATCH /admin/org/org-code`` and
    ``POST /admin/org/org-code/regenerate``.
    """

    org_code: str = Field(
        ...,
        description="The organization's current join code.",
        examples=["K7M2Q9X4"],
    )
    join_enabled: bool = Field(
        ...,
        description="Whether the org accepts new members via org-code join.",
        examples=[True],
    )


class UpdateOrgJoinRequest(BaseModel):
    """Request body for ``PATCH /admin/org/org-code``.

    Toggles org-code self-registration for the organization.  The field is
    mandatory — a missing ``join_enabled`` fails with 422.
    """

    join_enabled: bool = Field(
        ...,
        description="Whether the org accepts new members via org-code join.",
        examples=[False],
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


class InviteRequest(BaseModel):
    """Request body for ``POST /v1/admin/users/invite``.

    The invitee is created as a pending member (``password_hash`` NULL,
    ``invite_token_hash`` set) and receives the magic-link email.  Both
    fields are mandatory.
    """

    email: EmailStr = Field(
        ...,
        description="Email address of the invitee (globally unique).",
        examples=["alice@acme.com"],
    )
    name: str = Field(
        ...,
        max_length=512,
        description="Display name for the invitee.",
        examples=["Alice Johnson"],
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Strip surrounding whitespace and require a non-empty name."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        return stripped


class InviteResponse(BaseModel):
    """Response body for ``POST /v1/admin/users/invite``.

    Deliberately excludes the raw invite token — it is delivered to the
    invitee by email only and must never travel back over this API.
    """

    id: UUID = Field(..., description="The pending user's UUID.")
    email: EmailStr = Field(..., description="Invitee email address.")
    name: str = Field(..., description="Invitee display name.")


class InviteTokenRequest(BaseModel):
    """Request body for ``POST /v1/auth/invites/info``.

    The token travels in the POST body — never in the URL path — so the
    magic link (a live bearer credential) cannot leak into access logs.
    """

    token: str = Field(
        ...,
        min_length=1,
        description="The magic-link token from the invite email.",
    )


class InviteInfoResponse(BaseModel):
    """Response body for ``POST /v1/auth/invites/info``.

    Shown on the invite landing page before the invitee sets a password.
    Contains no secrets — safe to return for any valid (unexpired) token.
    """

    org_name: str = Field(..., description="Inviting organization's name.")
    email: EmailStr = Field(..., description="Invitee email address.")
    name: str = Field(..., description="Invitee display name.")


class AcceptInviteRequest(BaseModel):
    """Request body for ``POST /v1/auth/invites/accept``.

    Claims the invite atomically (single conditional UPDATE) and returns a
    JWT pair — the invitee is logged in immediately after setting a password.
    """

    token: str = Field(
        ...,
        min_length=1,
        description="The magic-link token from the invite email.",
    )
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="New password (min 8 chars, max 128).",
        examples=["secure-p@ssword-123"],
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
