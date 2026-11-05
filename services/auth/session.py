"""Auth session — login, MFA verify/enable/disable."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from core.exceptions import (
    AuthenticationError,
    NotFoundError,
)
from schemas.auth import (
    LoginRequest,
    LoginResponse,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaVerifyRequest,
    TokenResponse,
)
from schemas.email import OtpResponse

logger = logging.getLogger(__name__)

_MFA_SESSION_TTL_SEC = 600  # 10 minutes — MFA pending session lifetime


class SessionMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _org_repo: Any
    _email_service: Any
    _bao_client: Any

    # ── Login ───────────────────────────────────────────────────────────────

    async def login(self, payload: LoginRequest) -> LoginResponse:
        """Authenticate a dashboard user and return tokens or MFA challenge.

        If the user has MFA disabled, behaves as before and returns tokens.
        If MFA is enabled, sends an OTP, creates a pending session in Redis,
        and returns an MFA challenge response.

        Args:
            payload: Login request with email and password.

        Returns:
            A ``LoginResponse`` — either with tokens (MFA off) or
            ``requires_mfa=True`` with an ``mfa_session_token`` (MFA on).
        """
        user = await self._repo.find_user_by_email(payload.email)
        from services import auth_service as _facade  # via facade (patchable)
        if user is None:
            raise AuthenticationError("Invalid email or password.")

        if user.password_hash is None:
            raise AuthenticationError(
                "This user does not have password authentication enabled."
            )

        if not _facade.verify_password(payload.password, user.password_hash):
            raise AuthenticationError("Invalid email or password.")

        if not user.is_active or user.is_deleted:
            raise AuthenticationError("This account has been deactivated.")

        if not user.is_email_verified:
            raise AuthenticationError(
                "Email not verified. Please check your inbox for the "
                "verification code, or request a new one."
            )

        role = user.role if user.role is not None else "member"
        # ── MFA gate ─────────────────────────────────────────────────────────
        if user.mfa_enabled:
            session_token = _facade.secrets.token_hex(32)

            # Send MFA OTP FIRST — if this fails, no session is orphaned
            await self._otp_service.generate_and_send(
                email=payload.email,
                purpose="mfa",
            )

            # Store pending MFA session in Redis
            redis_key = f"mfa:session:{session_token}"
            session_data = {
                "user_id": str(user.id),
                "org_id": str(user.organization_id),
                "role": role,
                # Bound at creation: the OTP is verified against this address,
                # never against caller-supplied input (stops session fixation
                # where an attacker verifies their own OTP for a victim login).
                "email": payload.email,
            }
            await self._redis.setex(
                redis_key,
                _MFA_SESSION_TTL_SEC,
                json.dumps(session_data),
            )

            return LoginResponse(
                requires_mfa=True,
                mfa_session_token=session_token,
            )

        # ── Normal login (MFA disabled) ──────────────────────────────────────
        tokens = await self.issue_tokens(
            user_id=user.id,
            organization_id=user.organization_id,
            role=role,
        )
        return LoginResponse(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            token_type=tokens.token_type,
            requires_mfa=False,
        )
    # ── MFA verify ─────────────────────────────────────────────────────────

    async def mfa_verify(self, payload: MfaVerifyRequest) -> TokenResponse:
        """Complete MFA-authenticated login by verifying the OTP.

        Flow:
        1. Retrieve and validate the pending MFA session from Redis.
        2. Verify the OTP against the stored hash (purpose="mfa").
        3. Issue JWT tokens.

        Args:
            payload: Email, OTP code, and MFA session token.

        Returns:
            A ``TokenResponse`` with access and refresh tokens.

        Raises:
            AuthenticationError: If the session token is invalid/expired,
                or the OTP is invalid.
        """
        # Validate MFA session
        redis_key = f"mfa:session:{payload.mfa_session_token}"
        session_raw = await self._redis.get(redis_key)

        if session_raw is None:
            raise AuthenticationError(
                "MFA session has expired or is invalid. "
                "Please log in again."
            )

        session_data = json.loads(session_raw)
        await self._redis.delete(redis_key)  # single-use

        # Fail closed on sessions minted before the email binding existed.
        session_email = session_data.get("email")
        if not session_email:
            raise AuthenticationError(
                "MFA session has expired or is invalid. Please log in again."
            )

        # Verify OTP against the session-bound email — payload.email is
        # ignored so a caller cannot substitute their own address.
        verified = await self._otp_service.verify(
            email=session_email,
            purpose="mfa",
            code=payload.otp,
        )
        if not verified:
            raise AuthenticationError(
                "Invalid or expired MFA code. "
                "Please request a new code during login.",
            )

        # Issue tokens
        user_id = uuid.UUID(session_data["user_id"])
        org_id = uuid.UUID(session_data["org_id"])
        role = session_data["role"]

        return await self.issue_tokens(
            user_id=user_id,
            organization_id=org_id,
            role=role,
        )
    # ── MFA enable / disable ───────────────────────────────────────────────

    async def enable_mfa(
        self,
        user_id: uuid.UUID,
        payload: MfaEnableRequest,
    ) -> OtpResponse:
        """Enable MFA for a dashboard user.

        Requires password re-authentication.  Sends a confirmation OTP
        as a notification (the user does not need to verify it to complete
        the flow).

        Args:
            user_id: The authenticated user's UUID.
            payload: Current password for re-auth.

        Returns:
            An ``OtpResponse`` confirming MFA was enabled.
        """
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")
        from services import auth_service as _facade  # via facade (patchable)
        if user.password_hash is None or not _facade.verify_password(
            payload.password, user.password_hash,
        ):
            raise AuthenticationError("Current password is incorrect.")

        await self._repo.set_mfa_enabled(user_id, enabled=True)

        # Send confirmation email
        await self._otp_service.generate_and_send(
            email=user.email or "",
            purpose="mfa",
        )

        return OtpResponse(
            message="MFA has been enabled. "
            "Future logins will require a verification code sent to your email.",
        )

    async def disable_mfa(
        self,
        user_id: uuid.UUID,
        payload: MfaDisableRequest,
    ) -> OtpResponse:
        """Disable MFA for a dashboard user.

        Requires password re-authentication AND an MFA OTP to ensure the
        user still has access to their email.

        Args:
            user_id: The authenticated user's UUID.
            payload: Current password and MFA OTP.

        Returns:
            An ``OtpResponse`` confirming MFA was disabled.
        """
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")
        from services import auth_service as _facade  # via facade (patchable)
        if user.password_hash is None or not _facade.verify_password(
            payload.password, user.password_hash,
        ):
            raise AuthenticationError("Current password is incorrect.")

        verified = await self._otp_service.verify(
            email=user.email or "",
            purpose="mfa",
            code=payload.otp,
        )
        if not verified:
            raise AuthenticationError(
                "Invalid MFA code. Please request a new code.",
            )

        await self._repo.set_mfa_enabled(user_id, enabled=False)
        await self._otp_service.invalidate(email=user.email or "", purpose="mfa")

        return OtpResponse(
            message="MFA has been disabled.",
        )
