"""Pydantic schemas for email-OTP authentication flows.

Covers email verification, passwordless login, password reset, and MFA
challenge requests/responses.  All schemas are request/response models
only — never stored or logged in plaintext.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class SendOtpRequest(BaseModel):
    """Request body for sending an OTP to an email address.

    Used by:
    - ``POST /v1/auth/verify-email/send``
    - ``POST /v1/auth/login/otp/send``
    - ``POST /v1/auth/forgot-password``
    """

    email: EmailStr = Field(
        ...,
        description="Email address to receive the one-time passcode.",
        examples=["user@example.com"],
    )


class VerifyOtpRequest(BaseModel):
    """Request body for verifying an OTP code.

    Used by:
    - ``POST /v1/auth/verify-email/verify``
    - ``POST /v1/auth/login/otp/verify``
    - ``POST /v1/auth/reset-password``
    """

    email: EmailStr = Field(
        ...,
        description="Email address the OTP was sent to.",
        examples=["user@example.com"],
    )
    otp: str = Field(
        ...,
        min_length=4,
        max_length=8,
        description="The one-time passcode received via email.",
        examples=["483926"],
    )


class ResetPasswordRequest(BaseModel):
    """Request body for ``POST /v1/auth/reset-password``.

    Combines OTP verification with the new password setting.
    """

    email: EmailStr = Field(
        ...,
        description="Email address the OTP was sent to.",
        examples=["user@example.com"],
    )
    otp: str = Field(
        ...,
        min_length=4,
        max_length=8,
        description="The one-time passcode received via email.",
        examples=["483926"],
    )
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="New password (min 8 chars).",
    )


class OtpResponse(BaseModel):
    """Response returned after sending or verifying an OTP.

    Attributes:
        message: Human-readable status message.
    """

    message: str = Field(
        ...,
        description="Human-readable status.",
        examples=["OTP sent to email"],
    )
