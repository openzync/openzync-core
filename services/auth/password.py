"""Auth password — change, forgot, reset, strength check."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.exceptions import (
    AuthenticationError,
    NotFoundError,
    ValidationError,
)
from schemas.auth import (
    ChangePasswordRequest,
    TokenResponse,
)
from schemas.email import OtpResponse, ResetPasswordRequest

logger = logging.getLogger(__name__)

class PasswordMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _org_repo: Any
    _email_service: Any
    _bao_client: Any

    # ── Password change (first-login gate) ────────────────────────────────

    async def change_password(
        self,
        user_id: uuid.UUID,
        payload: ChangePasswordRequest,
    ) -> TokenResponse:
        """Change the user's password and clear the must-change gate.

        First-login flow for the seeded root credential: verify the old
        password, set the new hash, clear ``must_change_password``, rotate
        the refresh-token family (all existing sessions are revoked) and
        return a fresh token pair so the user stays signed in.

        Args:
            user_id: The authenticated user's UUID.
            payload: Old + new password.

        Returns:
            A fresh ``TokenResponse`` for the newly-rotated session.

        Raises:
            NotFoundError: If the user no longer exists.
            AuthenticationError: If the old password is wrong.
            ValidationError: If the new password is too weak.
        """
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        if user.password_hash is None:
            raise ValidationError(
                "This account does not have a password set."
            )
        from services import auth_service as _facade  # via facade (patchable)
        if not _facade.verify_password(payload.old_password, user.password_hash):
            raise AuthenticationError("Current password is incorrect.")

        from services import auth_service as _facade  # via facade (patchable)
        self._validate_password(payload.new_password)
        new_hash = _facade.hash_password(payload.new_password)

        await self._repo.update_dashboard_user(
            user_id=user_id,
            password_hash=new_hash,
            must_change_password=False,
        )
        # Rotate the refresh-token family — every prior session is dead,
        # including the one that logged the default credential in.
        await self._repo.revoke_all_refresh_tokens(user_id)
        await _facade.invalidate_must_change_password(
            self._redis, user.organization_id, user_id
        )

        logger.info(
            "auth.password_changed",
            extra={"user_id": str(user_id)},
        )
        return await self.issue_tokens(
            user_id=user_id,
            organization_id=user.organization_id,
            role=user.role if user.role is not None else "member",
        )
    # ── Password reset ─────────────────────────────────────────────────────

    async def forgot_password(self, email: str) -> OtpResponse:
        """Send a password-reset OTP to the user's email.

        Returns the same confirmation whether or not the account exists
        (prevents email enumeration); an OTP is only sent for existing
        accounts with a password set.

        Args:
            email: The email address requesting a reset.

        Returns:
            An ``OtpResponse`` confirming the code was sent.
        """
        user = await self._repo.find_user_by_email(email)
        if user is None or user.password_hash is None:
            logger.warning(
                "security.forgot_password_unknown_email",
                extra={"email": email},
            )
            return OtpResponse(
                message="If an account exists with this email, "
                "a password reset code has been sent.",
            )

        await self._otp_service.generate_and_send(
            email=email,
            purpose="password_reset",
        )

        return OtpResponse(
            message="If an account exists with this email, "
            "a password reset code has been sent.",
        )
    async def reset_password(self, payload: ResetPasswordRequest) -> OtpResponse:
        """Reset the user's password using an OTP-verified request.

        Flow:
        1. Verify the OTP against the stored hash in Redis.
        2. Validate and hash the new password.
        3. Update the user's ``password_hash``.
        4. Invalidate the OTP so it cannot be reused.
        5. Revoke all existing refresh tokens (force re-login).

        Args:
            payload: Email, OTP code, and new password.

        Returns:
            An ``OtpResponse`` confirming the password was changed.

        Raises:
            AuthenticationError: If the OTP is invalid or expired — raised
                for unknown emails too, so the response is indistinguishable
                from a wrong code against an existing account (anti
                account-enumeration).
            ValidationError: If the new password is too weak.
        """
        user = await self._repo.find_user_by_email(payload.email)
        if user is None or not user.is_active or user.is_deleted:
            raise AuthenticationError(
                "Invalid or expired reset code. "
                "Please request a new code.",
            )

        # Bind the session to the caller's tenant — the password update and
        # the refresh-token revocation below are org-scoped.
        await self._org_repo.set_tenant_context(user.organization_id)

        # Verify OTP
        verified = await self._otp_service.verify(
            email=payload.email,
            purpose="password_reset",
            code=payload.otp,
        )
        if not verified:
            raise AuthenticationError(
                "Invalid or expired reset code. "
                "Please request a new code.",
            )

        # Validate and hash new password
        from services import auth_service as _facade  # via facade (patchable)
        self._validate_password(payload.new_password)
        new_hash = _facade.hash_password(payload.new_password)

        # Update password hash
        await self._repo.update_dashboard_user(
            user_id=user.id,
            password_hash=new_hash,
        )

        # Revoke all refresh tokens to force re-login
        await self._repo.revoke_all_refresh_tokens(user.id)

        # Invalidate OTP so it cannot be reused
        await self._otp_service.invalidate(
            email=payload.email,
            purpose="password_reset",
        )

        return OtpResponse(
            message="Your password has been reset successfully. "
            "Please log in with your new password.",
        )
    @staticmethod
    def _validate_password(password: str) -> None:
        """Validate password meets minimum strength requirements.

        Args:
            password: The plaintext password.

        Raises:
            ValidationError: If the password is too weak.
        """
        if len(password) < 8:
            raise ValidationError(
                "Password must be at least 8 characters long."
            )
        if not any(c.isupper() for c in password):
            raise ValidationError(
                "Password must contain at least one uppercase letter."
            )
        if not any(c.islower() for c in password):
            raise ValidationError(
                "Password must contain at least one lowercase letter."
            )
        if not any(c.isdigit() for c in password):
            raise ValidationError(
                "Password must contain at least one digit."
            )
