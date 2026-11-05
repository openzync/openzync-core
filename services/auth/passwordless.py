"""Auth passwordless — email-OTP login."""

from __future__ import annotations

import logging
from typing import Any

from core.exceptions import (
    AuthenticationError,
)
from schemas.auth import (
    TokenResponse,
)
from schemas.email import OtpResponse, VerifyOtpRequest

logger = logging.getLogger(__name__)

class PasswordlessMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _org_repo: Any
    _email_service: Any
    _bao_client: Any

    # ── Passwordless login ─────────────────────────────────────────────────

    async def generate_login_otp(self, email: str) -> OtpResponse:
        """Send a passwordless login OTP to the user's email.

        Returns the same confirmation whether or not the account exists
        (prevents email enumeration); an OTP is only sent for existing
        accounts.

        Args:
            email: The email address requesting a login code.

        Returns:
            An ``OtpResponse`` confirming the code was sent.
        """
        user = await self._repo.find_user_by_email(email)
        if user is None or not user.is_active or user.is_deleted:
            logger.warning(
                "security.login_otp_send_unknown_email",
                extra={"email": email},
            )
            return OtpResponse(
                message="If an account exists with this email, "
                "a login code has been sent.",
            )

        await self._otp_service.generate_and_send(
            email=email,
            purpose="passwordless_login",
        )

        return OtpResponse(
            message="If an account exists with this email, "
            "a login code has been sent.",
        )

    async def passwordless_login(self, payload: VerifyOtpRequest) -> TokenResponse:
        """Authenticate a user via email OTP (no password required).

        Flow:
        1. Find user by email.
        2. Verify the OTP against the stored hash in Redis.
        3. Auto-verify the email if not already verified (OTP proves ownership).
        4. Invalidate the OTP so it cannot be reused.
        5. Issue and return JWT access + refresh tokens.

        Args:
            payload: Email and OTP code.

        Returns:
            A ``TokenResponse`` with access and refresh tokens.

        Raises:
            AuthenticationError: If the OTP is invalid or expired — or the
                email has no account (indistinguishable, anti-enumeration).
        """
        user = await self._repo.find_user_by_email(payload.email)
        if user is None or not user.is_active or user.is_deleted:
            # No account → no OTP was ever issued for it.  Raise the exact
            # wrong-code error so missing vs existing emails are indistinguishable.
            raise AuthenticationError(
                "Invalid or expired login code. "
                "Please request a new code."
            )

        # Bind the session to the caller's tenant — the auto-verification
        # update and the refresh-token insert below are org-scoped.
        await self._org_repo.set_tenant_context(user.organization_id)

        verified = await self._otp_service.verify(
            email=payload.email,
            purpose="passwordless_login",
            code=payload.otp,
        )
        if not verified:
            raise AuthenticationError(
                "Invalid or expired login code. "
                "Please request a new code.",
            )

        # Auto-verify email if this is the user's first login
        if not user.is_email_verified:
            await self._repo.mark_email_verified(user.id)

        # Invalidate OTP so it cannot be reused
        await self._otp_service.invalidate(
            email=payload.email,
            purpose="passwordless_login",
        )

        return await self.issue_tokens(
            user_id=user.id,
            organization_id=user.organization_id,
            role=user.role if user.role is not None else "member",
        )
