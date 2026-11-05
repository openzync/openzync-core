"""Auth org join — org-code join, email verify, resend."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ValidationError,
)
from core.org_codes import normalize_org_code
from core.rbac import (
    MEMBER_DEFAULT_PERMISSIONS,
)
from schemas.auth import (
    JoinRequest,
    SignupResponse,
    TokenResponse,
    VerifyEmailRequest,
)

logger = logging.getLogger(__name__)

class OrgJoinMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _org_repo: Any
    _email_service: Any
    _bao_client: Any

    # ── Org-code join ────────────────────────────────────────────────────────

    async def join_organization(self, payload: JoinRequest) -> SignupResponse:
        """Join an existing organization via its join code.

        Flow:
        1. Normalize the org code and look up the active organization.
        2. If the email is already registered, return the generic response
           (no user created, no OTP sent — anti-enumeration, mirrors signup).
        3. Hash the password and create a **member** dashboard user in the
           target organization.
        4. Send a signup OTP to the user's email.

        The response is identical whether or not the email already exists.
        An invalid org code is the one case that fails loudly — the code is
        the credential being presented, and there is no enumeration risk in
        rejecting it.

        Args:
            payload: Email, password, and org code.

        Returns:
            A ``SignupResponse`` with a confirmation message.

        Raises:
            ValidationError: If the org code is unknown or the org is inactive.
            AuthorizationError: If the org has disabled org-code
                self-registration (``join_enabled`` False → 403), or the
                platform policy is ``reject_all`` (403).
        """
        # ── Platform policy gate — sits on top of the per-org join_enabled
        #    check; reject_all blocks every channel. ────────────────────────
        from services import auth_service as _facade  # via facade (patchable)
        system_config = await _facade.get_system_config(
            self._redis, self._bao_client
        )
        if system_config.org_creation_policy == "reject_all":
            raise AuthorizationError("Registration is disabled")

        code = normalize_org_code(payload.org_code)
        # Bind the join code to the session: it is the credential presented
        # by an anonymous joiner, and the ``org_join_code_lookup`` policy
        # exposes exactly the organization row that owns it.
        await self._org_repo.set_lookup_code(code)
        org = await self._org_repo.get_by_code(code)
        if org is None:
            raise ValidationError("Invalid organization code")
        if not org.join_enabled:
            # Admin-disabled self-registration: the code is valid but the
            # org is not accepting new members — distinct from an unknown
            # code (422 above), so admins can see join attempts in the logs.
            logger.warning(
                "auth.join_disabled",
                extra={"org_id": str(org.id)},
            )
            raise AuthorizationError(
                "This organization is not accepting new members"
            )

        # Bind the session to the resolved tenant — the member insert below
        # is org-scoped and the request has no org context of its own.
        await self._org_repo.set_tenant_context(org.id)

        existing = await self._repo.find_user_by_email(payload.email)
        if existing is not None:
            logger.warning(
                "security.join_existing_email",
                extra={"email": payload.email},
            )
            return self._signup_success_response(payload.email)

        pw_hash = _facade.hash_password(payload.password)
        try:
            await self._repo.create_dashboard_user(
                organization_id=org.id,
                email=payload.email,
                password_hash=pw_hash,
                name=payload.email.split("@")[0],  # default name from email
                role="member",
                permissions=list(MEMBER_DEFAULT_PERMISSIONS),
            )
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
                extra={"email": payload.email},
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

        # Bind the session to the caller's tenant — the email-verification
        # update and the refresh-token insert below are org-scoped.
        await self._org_repo.set_tenant_context(user.organization_id)

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

        # Bootstrap OpenBao namespace for the org (idempotent — skips if exists)
        if self._bao_client is not None:
            try:
                await self._bao_client.create_org_namespace(
                    user.organization_id
                )
            except Exception:
                logger.exception(
                    "auth_service.openbao_ns_bootstrap_failed org_id=%s",
                    str(user.organization_id),
                )

        # Issue tokens now that email is verified
        return await self.issue_tokens(
            user_id=user.id,
            organization_id=user.organization_id,
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
                extra={"email": email},
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
