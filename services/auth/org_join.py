"""Auth org join — singleton-org join, email verify, resend."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from core.openbao_exceptions import OpenBaoConnectionError
from core.rls import set_user_context
from core.rbac import (
    MEMBER_DEFAULT_PERMISSIONS,
)
from repositories.instance_settings_repository import InstanceSettingsRepository
from schemas.auth import (
    JoinRequest,
    SignupResponse,
    TokenResponse,
    VerifyEmailRequest,
)
from utils.password import hash_password
from utils.pii import mask_email

logger = logging.getLogger(__name__)

class OrgJoinMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _email_service: Any
    _bao_client: Any
    _settings_repo: Any

    def _settings(self) -> InstanceSettingsRepository:
        """Return the injected settings repo (fallback constructs one)."""
        if self._settings_repo is not None:
            return self._settings_repo
        return InstanceSettingsRepository(self._repo.session)

    # ── Org-code join ────────────────────────────────────────────────────────

    async def join_organization(self, payload: JoinRequest) -> SignupResponse:
        """Join the singleton platform organization as a member.

        Single platform org: the join code is gone — every join lands in
        the platform org.  Registration is governed by
        ``instance_settings.registration_mode``: ``open`` creates a live
        member, ``approval`` creates an inactive member pending admin
        activation, ``invite_only`` rejects with 403.

        The response is identical whether or not the email already exists.

        Args:
            payload: Email and password.

        Returns:
            A ``SignupResponse`` with a confirmation message.

        Raises:
            AuthorizationError: If registration is ``invite_only`` or the
                platform org is missing.
        """
        # ── Platform policy gate ──────────────────────────────────────────
        mode = await self._settings().get_registration_mode()
        if mode == "invite_only":
            raise AuthorizationError("Registration is invite-only")
        if mode not in ("open", "approval"):
            raise AuthorizationError("Registration is disabled")

        # No org gate — the single-user model has no platform org.
        # Join mints ``member`` (unlike open signup, which mints the
        # bootstrapping ``admin``) — joined users never self-promote.
        existing = await self._repo.find_user_by_email(payload.email)
        if existing is not None:
            logger.warning(
                "security.join_existing_email",
                extra={"email": mask_email(payload.email)},
            )
            return self._signup_success_response(payload.email)

        # bcrypt blocks the event loop — offload to a worker thread.
        pw_hash = await asyncio.to_thread(hash_password, payload.password)
        try:
            user = await self._repo.create_dashboard_user(
                email=payload.email,
                password_hash=pw_hash,
                name=payload.email.split("@")[0],  # default name from email
                role="member",
                permissions=list(MEMBER_DEFAULT_PERMISSIONS),
            )
            await set_user_context(self._repo.session, user.id)
            if mode == "approval":
                user.is_active = False
                await self._repo.flush()
            else:
                await self._otp_service.generate_and_send(
                    email=payload.email,
                    purpose="signup",
                )
        except IntegrityError:
            # Concurrent duplicate join — the unique email index won.  Same
            # generic response as a fresh join; roll back the aborted
            # transaction so the session is usable.
            logger.warning(
                "security.join_existing_email",
                extra={"email": mask_email(payload.email)},
            )
            await self._repo.rollback()
            return self._signup_success_response(payload.email)

        return self._signup_success_response(payload.email)
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

        # Bootstrap OpenBao namespace for the org (idempotent — skips if exists).
        # Fail loud, never silent: a missing client means every subsequent
        # config write breaks, so raise instead of skipping.
        if self._bao_client is None:
            raise OpenBaoConnectionError(
                "OpenBao client required to bootstrap the org namespace"
            )
        await self._bao_client.create_org_namespace(user.id)

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
