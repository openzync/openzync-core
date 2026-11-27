"""Auth member email verification — verify email, resend OTP."""

from __future__ import annotations

import logging
from typing import Any

from core.exceptions import AuthenticationError
from core.rls import set_user_context
from schemas.auth import (
    SignupResponse,
    TokenResponse,
    VerifyEmailRequest,
)
from utils.pii import mask_email

logger = logging.getLogger(__name__)


class MemberSignupMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _email_service: Any
    _settings_repo: Any

    async def verify_email(
        self,
        payload: VerifyEmailRequest,
    ) -> TokenResponse:
        """Verify a user's email address with the OTP and issue tokens.

        Flow:
        1. Verify the OTP against the stored hash in Redis.
        2. Mark the user's email as verified in the database.
        3. Issue and return JWT access + refresh tokens.

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
                "Invalid or expired verification code. "
                "Please request a new code."
            )

        # Bind the session to the caller's user — the email-verification
        # update and the refresh-token insert below are user-scoped.
        await set_user_context(self._repo.session, user.id)

        # Always verify the OTP — even for already-verified users.
        # This prevents an unauthenticated attacker who knows a verified
        # email from obtaining JWT tokens (privilege escalation).
        verified = await self._otp_service.verify(
            email=payload.email,
            purpose="signup",
            code=payload.otp,
        )
        if not verified:
            raise AuthenticationError(
                "Invalid or expired verification code. "
                "Please request a new code."
            )

        # Only update DB if email was not already verified
        if not user.is_email_verified:
            await self._repo.mark_email_verified(user.id)

        # Issue tokens now that email is verified
        return await self.issue_tokens(
            user_id=user.id,
            role=user.role if user.role is not None else "member",
        )
    async def resend_verification(self, email: str) -> SignupResponse:
        """Resend the email verification OTP.

        Returns the same generic confirmation for missing, verified, and
        unverified accounts so the endpoint cannot be used to enumerate
        accounts.  An OTP is actually sent only for unverified accounts.

        Rate limiting is handled internally by the OtpService (cooldown
        and hourly send cap).

        Args:
            email: The email address registered during signup.

        Returns:
            A ``SignupResponse`` confirming the code was sent.
        """
        generic = SignupResponse(
            message="If an account exists with this email, "
            "a verification code has been sent.",
            email=email,
        )

        user = await self._repo.find_user_by_email(email)
        if user is None:
            logger.warning(
                "security.resend_otp_unknown_email",
                extra={"email": mask_email(email)},
            )
            return generic

        if not user.is_email_verified:
            await self._otp_service.generate_and_send(
                email=email,
                purpose="signup",
            )

        return generic
    @staticmethod
    def _signup_success_response(email: str) -> SignupResponse:
        """Build the generic signup confirmation (anti-enumeration).

        Args:
            email: The email address submitted by the client.

        Returns:
            A ``SignupResponse`` identical to a fresh-signup response.
        """
        return SignupResponse(
            message="Verification code sent to email. "
            "Use POST /v1/auth/verify-email to complete signup.",
            email=email,
        )
