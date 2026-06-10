"""Pydantic schemas for dashboard authentication.

Covers signup, login, token refresh, and the token response format.
All auth schemas are request/response models — never stored or logged directly.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


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

    model_config = {"from_attributes": True}


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
