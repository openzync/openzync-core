"""Auth service — dashboard user signup, login, and token refresh.

All business logic for email/password authentication lives here.
The service layer orchestrates the auth repository, password hashing,
JWT creation, and refresh token rotation.

Responsibilities:
- Signup: create org → create admin user → return JWT pair.
- Login: find user by email → verify password → return JWT pair.
- Refresh: verify refresh token → rotate → return new JWT pair.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from core.locales import ALLOWED_LOCALES
from core.org_codes import normalize_org_code
from core.rbac import invalidate_must_change_password
from core.system_config import get_system_config
from repositories.auth_repository import AuthRepository  # noqa: TC001
from repositories.organization_repository import (  # noqa: TC001
    OrganizationRepository,
)
from schemas.auth import (
    ChangePasswordRequest,
    DashboardUserResponse,
    JoinRequest,
    LoginRequest,
    LoginResponse,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaVerifyRequest,
    PendingOrgResponse,
    SignupRequest,
    SignupResponse,
    TokenResponse,
    UpdateProfileRequest,
    VerifyEmailRequest,
)
from schemas.email import OtpResponse, ResetPasswordRequest, VerifyOtpRequest
from utils.crypto import create_jwt_token
from utils.password import hash_password, verify_password

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

    from core.openbao import OpenBaoClient
    from services.email_service import EmailService
    from services.otp_service import OtpService

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

def _access_token_ttl() -> timedelta:
    """Lazy access to JWT access token TTL from settings."""
    return timedelta(minutes=get_settings().JWT_ACCESS_TOKEN_TTL_MINUTES)


def _refresh_token_ttl() -> timedelta:
    """Lazy access to JWT refresh token TTL from settings."""
    return timedelta(days=get_settings().JWT_REFRESH_TOKEN_TTL_DAYS)

_JWT_ALGORITHM = "HS256"
_MFA_SESSION_TTL_SEC = 600  # 10 minutes — MFA pending session lifetime
_REFRESH_FAMILY_WALK_LIMIT = 30  # bounded loop for rotation-family revocation


# ═══════════════════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════════════════


class AuthService:
    """Handles dashboard authentication flows.

    Args:
        repo: Repository for auth-related DB access.
        otp_service: OTP service for email verification and MFA.
        redis: Async Redis client for MFA session storage.
        org_repo: Repository for organization lookups (org-code join).
        email_service: Optional email service for notification-only emails
            (e.g. password-change confirmation).  ``None`` skips notifications.
        bao_client: Optional OpenBao client for bootstrapping org namespaces
            during email verification.
    """

    def __init__(
        self,
        repo: AuthRepository,
        otp_service: OtpService,  # noqa: F821
        redis: AsyncRedis,  # noqa: F821
        org_repo: OrganizationRepository,
        email_service: EmailService | None = None,  # noqa: F821
        bao_client: OpenBaoClient | None = None,  # noqa: F821
    ) -> None:
        self._repo = repo
        self._otp_service = otp_service
        self._redis = redis
        self._org_repo = org_repo
        self._email_service = email_service
        self._bao_client = bao_client

    # ── Signup ──────────────────────────────────────────────────────────────

    async def signup(
        self, payload: SignupRequest
    ) -> SignupResponse | PendingOrgResponse:
        """Create a new organization with an admin dashboard user.

        Platform policy gate (``get_system_config``) runs FIRST:
        - ``reject_all`` → 403, no org is created.
        - ``approvals`` with ``public_signup`` in scope → the org is
          created as ``pending`` (approval queue) with a pending admin
          user — no OpenBao namespace, no OTP, no live access.
        - ``approvals`` WITHOUT ``public_signup`` in scope → 403.  A
          non-selected channel is rejected, never silently routed to
          instant creation.
        - ``allow_all`` → the current live-org flow below.

        Live flow:
        1. Check email uniqueness (no existing user with this email).
        2. Create the organization.
        3. Hash the password and create the dashboard admin user.
        4. Send an OTP verification code to the user's email.
        5. Return a confirmation message (no tokens — user must verify email).

        The response is identical whether or not the email is already
        registered — the real distinction is logged server-side, so the
        endpoint cannot be used to enumerate existing accounts.

        Args:
            payload: Signup request with email, password, org name.

        Returns:
            A ``SignupResponse`` with a confirmation message, or a
            ``PendingOrgResponse`` when the org enters the approval queue.

        Raises:
            AuthorizationError: If registration is disabled (``reject_all``)
                or the approvals policy excludes the public signup channel.
            ValidationError: If the password does not meet requirements,
                or the org name is reserved (``SYSTEM``).
        """
        # ── Platform policy gate (before any org creation) ────────────────
        system_config = await get_system_config(
            self._redis, self._bao_client
        )
        if system_config.org_creation_policy == "reject_all":
            raise AuthorizationError("Registration is disabled")

        # Reserved name check — the platform org owns "SYSTEM" exclusively.
        self._validate_org_name(payload.organization_name)

        # ── Approval-queue path ────────────────────────────────────────────
        if system_config.org_creation_policy == "approvals":
            # Non-selected channels are rejected — a scope that excludes
            # public signup must not fall through to instant org creation.
            if system_config.approval_scope not in ("public_signup", "both"):
                raise AuthorizationError("Registration is disabled")
            try:
                return await self.create_pending_org_and_admin(
                    organization_name=payload.organization_name,
                    admin_email=str(payload.email),
                    admin_name=payload.email.split("@")[0],
                )
            except IntegrityError:
                # Concurrent duplicate email in the approvals path — the
                # unique email index won.  Same generic success the live
                # path returns (anti-enumeration, smoke contract); roll
                # back the aborted transaction (the pending org row is
                # uncommitted) so the session stays usable.
                logger.warning(
                    "security.signup_existing_email",
                    extra={"email": payload.email},
                )
                await self._repo.rollback()
                return self._signup_success_response(payload.email)

        # ── Live path (allow_all only) ─────────────────────────────────────
        # Validate password strength
        self._validate_password(payload.password)

        # Check email uniqueness
        existing = await self._repo.find_user_by_email(payload.email)
        if existing is not None:
            logger.warning(
                "security.signup_existing_email",
                extra={"email": payload.email},
            )
            return self._signup_success_response(payload.email)

        # Create organization
        try:
            org = await self._repo.create_organization(
                name=payload.organization_name,
                plan="free",
            )

            # Seed default prompt templates for the new org
            await self._repo.seed_prompts_for_org(org.id)

            # Create dashboard admin user
            pw_hash = hash_password(payload.password)
            await self._repo.create_dashboard_user(
                organization_id=org.id,
                email=payload.email,
                password_hash=pw_hash,
                name=payload.email.split("@")[0],  # default name from email
                role="admin",
            )

            # Send verification OTP — no tokens issued until email is verified.
            await self._otp_service.generate_and_send(
                email=payload.email,
                purpose="signup",
            )
        except IntegrityError:
            # Concurrent duplicate signup — the unique email index won.
            # Same generic response as a fresh signup; roll back the aborted
            # transaction (the session is unusable until then) so no orphan
            # org is committed.
            logger.warning(
                "security.signup_existing_email",
                extra={"email": payload.email},
            )
            await self._repo.rollback()
            return self._signup_success_response(payload.email)

        return self._signup_success_response(payload.email)

    async def create_pending_org_and_admin(
        self,
        *,
        organization_name: str,
        admin_email: str,
        admin_name: str,
    ) -> PendingOrgResponse:
        """Create a pending org + pending admin user (approval queue).

        Shared by the signup approval path and the in-app org-request
        flow.  One transaction: the org row (``status='pending'``) and the
        admin row (``role='admin'``, ``password_hash=None``,
        ``invite_token_hash=None`` — NOT an invite yet).  No OpenBao
        namespace, no OTP — the org is not live until a superadmin
        approves it (``OrganizationService.approve_org`` mints the invite
        then).

        Args:
            organization_name: Desired org name (already validated != SYSTEM).
            admin_email: Email of the designated admin.
            admin_name: Display name for the designated admin.

        Returns:
            A ``PendingOrgResponse`` confirming the request is queued.

        Raises:
            ValidationError: If the org name is reserved (``SYSTEM``).
        """
        self._validate_org_name(organization_name)

        org = await self._repo.create_organization(
            name=organization_name,
            plan="free",
            status="pending",
        )
        # Pending admin — no password, no invite token yet.  The unique
        # email index (ix_user_email_unique) guarantees the admin email
        # cannot belong to any live user; a duplicate raises IntegrityError
        # which the caller maps to 409.
        await self._repo.create_dashboard_user(
            organization_id=org.id,
            email=admin_email,
            password_hash=None,
            name=admin_name,
            role="admin",
        )
        logger.info(
            "auth.org_pending",
            extra={"org_id": str(org.id), "email": admin_email},
        )
        return PendingOrgResponse(
            message=(
                "Your organization is pending approval. "
                "You will receive an email when it is approved."
            ),
        )

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
        if not verify_password(payload.old_password, user.password_hash):
            raise AuthenticationError("Current password is incorrect.")

        self._validate_password(payload.new_password)
        new_hash = hash_password(payload.new_password)

        await self._repo.update_dashboard_user(
            user_id=user_id,
            password_hash=new_hash,
            must_change_password=False,
        )
        # Rotate the refresh-token family — every prior session is dead,
        # including the one that logged the default credential in.
        await self._repo.revoke_all_refresh_tokens(user_id)
        await invalidate_must_change_password(self._redis, user_id)

        logger.info(
            "auth.password_changed",
            extra={"user_id": str(user_id)},
        )
        return await self.issue_tokens(
            user_id=user_id,
            organization_id=user.organization_id,
            role=user.role if user.role is not None else "member",
        )

    # ── Org-request shared helpers ─────────────────────────────────────────

    async def create_live_admin_user(
        self,
        organization_id: uuid.UUID,
        email: str,
        name: str | None,
    ) -> None:
        """Create a live admin user with no password (OTP-activated).

        Used by the in-app org-request ``allow_all`` path: the designated
        admin activates via email OTP (verify-email), then sets a real
        password via the reset flow.

        Args:
            organization_id: The (already live) organization UUID.
            email: The admin's email address.
            name: Optional display name.
        """
        await self._repo.create_dashboard_user(
            organization_id=organization_id,
            email=email,
            password_hash=None,
            name=name,
            role="admin",
        )

    @staticmethod
    def _validate_org_name(name: str) -> None:
        """Reject the reserved platform org name ``SYSTEM`` (case-insensitive).

        Args:
            name: The proposed organization name.

        Raises:
            ValidationError: If the name is reserved.
        """
        if name.strip().upper() == "SYSTEM":
            raise ValidationError(
                f"Organization name '{name}' is reserved and cannot be used."
            )

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
        system_config = await get_system_config(
            self._redis, self._bao_client
        )
        if system_config.org_creation_policy == "reject_all":
            raise AuthorizationError("Registration is disabled")

        code = normalize_org_code(payload.org_code)
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

        existing = await self._repo.find_user_by_email(payload.email)
        if existing is not None:
            logger.warning(
                "security.join_existing_email",
                extra={"email": payload.email},
            )
            return self._signup_success_response(payload.email)

        pw_hash = hash_password(payload.password)
        try:
            await self._repo.create_dashboard_user(
                organization_id=org.id,
                email=payload.email,
                password_hash=pw_hash,
                name=payload.email.split("@")[0],  # default name from email
                role="member",
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
        if user is None:
            # No account → no OTP was ever issued for it.  Raise the exact
            # wrong-code error so missing vs existing emails are indistinguishable.
            raise AuthenticationError(
                "Invalid or expired verification code. "
                "Please request a new code."
            )

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
                locale=user.locale,
            )

        return generic

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
            locale=user.locale,
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
        if user is None:
            raise AuthenticationError(
                "Invalid or expired reset code. "
                "Please request a new code.",
            )

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
        self._validate_password(payload.new_password)
        new_hash = hash_password(payload.new_password)

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
        if user is None:
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
            locale=user.locale,
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
        if user is None:
            # No account → no OTP was ever issued for it.  Raise the exact
            # wrong-code error so missing vs existing emails are indistinguishable.
            raise AuthenticationError(
                "Invalid or expired login code. "
                "Please request a new code."
            )

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
        if user is None:
            raise AuthenticationError("Invalid email or password.")

        if user.password_hash is None:
            raise AuthenticationError(
                "This user does not have password authentication enabled."
            )

        if not verify_password(payload.password, user.password_hash):
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
            session_token = secrets.token_hex(32)

            # Send MFA OTP FIRST — if this fails, no session is orphaned
            await self._otp_service.generate_and_send(
                email=payload.email,
                purpose="mfa",
                locale=user.locale,
            )

            # Store pending MFA session in Redis
            redis_key = f"mfa:session:{session_token}"
            session_data = {
                "user_id": str(user.id),
                "org_id": str(user.organization_id),
                "role": role,
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

        # Verify OTP
        verified = await self._otp_service.verify(
            email=payload.email,
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

        if user.password_hash is None or not verify_password(
            payload.password, user.password_hash,
        ):
            raise AuthenticationError("Current password is incorrect.")

        await self._repo.set_mfa_enabled(user_id, enabled=True)

        # Send confirmation email
        await self._otp_service.generate_and_send(
            email=user.email or "",
            purpose="mfa",
            locale=user.locale,
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

        if user.password_hash is None or not verify_password(
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

    # ── Refresh ─────────────────────────────────────────────────────────────

    async def refresh(self, raw_token: str) -> TokenResponse:
        """Rotate a refresh token and issue a new token pair.

        The presented token is claimed with a single conditional UPDATE,
        so exactly one of two concurrent requests with the same token
        wins.  A loser — or any replay of an already-rotated token —
        triggers revocation of the entire rotation family before a
        generic rejection, so a stolen token cannot keep derived tokens
        alive.

        Args:
            raw_token: The opaque refresh token string from the client.

        Returns:
            A new ``TokenResponse`` with fresh access and refresh tokens.

        Raises:
            AuthenticationError: If the refresh token is invalid, expired,
                reused, or the owning user is deactivated.
        """
        token_hash = self._hash_refresh_token(raw_token)

        # Atomic claim — a concurrent request with the same token loses here.
        if not await self._repo.revoke_refresh_token_if_current(token_hash):
            await self._revoke_family(token_hash)
            raise AuthenticationError(
                "Refresh token is invalid or has expired."
            )

        stored = await self._repo.get_refresh_token_by_hash(token_hash)
        if stored is None:
            # Unreachable: the conditional UPDATE only matches existing rows.
            raise AuthenticationError("Refresh token is invalid or has expired.")

        # Look up the user to get the actual role
        user_id = uuid.UUID(stored.user_id)
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise AuthenticationError("User no longer exists.")
        if not user.is_active or user.is_deleted:
            raise AuthenticationError("This account has been deactivated.")
        role = user.role if user.role is not None else "member"

        new_tokens = await self.issue_tokens(
            user_id=user_id,
            organization_id=stored.organization_id,
            role=role,
        )

        # Chain the claimed token to its successor (rotation audit trail).
        new_refresh_hash = self._hash_refresh_token(new_tokens.refresh_token)
        new_stored = await self._repo.get_refresh_token_by_hash(
            new_refresh_hash
        )
        if new_stored is not None:
            await self._repo.set_refresh_token_rotated_by(
                stored.id,
                new_stored.id,
            )

        return new_tokens

    async def _revoke_family(self, token_hash: str) -> None:
        """Revoke the entire rotation family of a reused refresh token.

        Walks the ``rotated_by`` successor chain from the presented token
        forward to the leaf and revokes every linked token.  Ancestors are
        already revoked by rotation, so this walk covers every live token
        in the family.  Bounded to ``_REFRESH_FAMILY_WALK_LIMIT`` hops.

        Args:
            token_hash: SHA-256 hash of the presented (rejected) token.
        """
        presented = await self._repo.get_refresh_token_by_hash(token_hash)
        if presented is None:
            return  # token never existed — nothing to walk

        logger.warning(
            "security.refresh_token_reuse",
            extra={
                "user_id": presented.user_id,
                "token_id": str(presented.id),
            },
        )

        family_ids: list[uuid.UUID] = [presented.id]
        seen: set[uuid.UUID] = {presented.id}
        node = presented
        for _ in range(_REFRESH_FAMILY_WALK_LIMIT):
            if node.rotated_by is None or node.rotated_by in seen:
                break
            seen.add(node.rotated_by)
            family_ids.append(node.rotated_by)
            successor = await self._repo.get_refresh_token_by_id(
                node.rotated_by
            )
            if successor is None:
                break
            node = successor

        await self._repo.revoke_refresh_token_ids(family_ids)
        # Persist the revocation NOW — the caller raises immediately after,
        # and the request-scoped session dependency would otherwise roll
        # the family kill back together with the error.
        await self._repo.commit()

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

    # ── Internal helpers ────────────────────────────────────────────────────

    async def issue_tokens(
        self,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        role: str,
    ) -> TokenResponse:
        """Generate and persist an access + refresh token pair.

        Public so sibling services (e.g. ``InviteService``) can log a user
        in without re-implementing JWT issuance.  All internal auth flows
        call this too — it is the single token-issuance path.

        The access token carries a ``mcp`` claim (must-change-password)
        **read from the DB row at issue time** — every issuance path
        (login, refresh, verify-email, invite-accept, change-password)
        gets the flag's current value.  Refresh never copies claims from
        the old token; a flag cleared by ``change_password`` produces
        ``mcp: false`` on the very next issuance.

        Args:
            user_id: The authenticated user's UUID.
            organization_id: The user's organization UUID.
            role: User role for JWT claims.

        Returns:
            A ``TokenResponse`` with fresh tokens.
        """
        # Must-change-password flag — read from the source of truth at
        # issue time so a rotated/stale token can never outlive the flag.
        user = await self._repo.get_user_by_id(user_id)
        must_change_password = (
            bool(user.must_change_password) if user is not None else False
        )

        # Use naive UTC datetime for DB storage (refresh_token.expires_at
        # is TIMESTAMP WITHOUT TIME ZONE).
        now = datetime.now(UTC).replace(tzinfo=None)

        # Access token
        access_token = create_jwt_token(
            data={
                "sub": str(user_id),
                "org_id": str(organization_id),
                "role": role,
                "mcp": must_change_password,
                "type": "access",
            },
            secret=get_settings().SECRET_KEY,
            expires_delta=_access_token_ttl(),
        )

        # Refresh token (opaque — stored as SHA-256 hash)
        raw_refresh = secrets.token_hex(32)
        refresh_hash = self._hash_refresh_token(raw_refresh)
        refresh_expires = now + _refresh_token_ttl()

        await self._repo.create_refresh_token(
            user_id=user_id,
            organization_id=organization_id,
            token_hash=refresh_hash,
            expires_at=refresh_expires,
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=raw_refresh,
            expires_in=int(_access_token_ttl().total_seconds()),
        )

    @staticmethod
    def _hash_refresh_token(raw: str) -> str:
        """Deterministic SHA-256 hash of a refresh token for DB storage.

        Args:
            raw: The opaque refresh token string.

        Returns:
            Hex-encoded SHA-256 digest.
        """
        return hashlib.sha256(raw.encode()).hexdigest()

    # ── Profile ──────────────────────────────────────────────────────────────

    async def get_profile(self, user_id: uuid.UUID) -> DashboardUserResponse:
        """Get the dashboard user's own profile.

        Args:
            user_id: The authenticated user's UUID (from JWT sub claim).

        Returns:
            The user's public profile.

        Raises:
            NotFoundError: If the user no longer exists.
        """
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")
        return DashboardUserResponse(
            id=user.id,
            email=user.email or "",
            name=user.name,
            role=user.role if user.role is not None else "member",
            organization_id=user.organization_id,
            is_email_verified=user.is_email_verified,
            mfa_enabled=user.mfa_enabled,
            must_change_password=bool(user.must_change_password),
            locale=user.locale,
        )

    async def update_profile(
        self,
        user_id: uuid.UUID,
        payload: UpdateProfileRequest,
    ) -> DashboardUserResponse:
        """Update the dashboard user's profile and/or password.

        Args:
            user_id: The authenticated user's UUID.
            payload: Fields to update. Only non-``None`` fields are applied.

        Returns:
            Updated user profile.

        Raises:
            NotFoundError: If the user no longer exists.
            ValidationError: If password change is requested without
                valid current password.
            ConflictError: If the new email is already taken.
        """
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        has_changes = False
        update_kwargs: dict[str, Any] = {}

        # Profile fields
        if payload.name is not None:
            update_kwargs["name"] = payload.name
            has_changes = True

        if payload.locale is not None:
            # Schema validator already rejects unknown tags for HTTP callers;
            # this guard also covers direct service invocations (tests, workers).
            if payload.locale not in ALLOWED_LOCALES:
                raise ValidationError(
                    f"Unsupported locale '{payload.locale}'. Supported: "
                    f"{', '.join(sorted(ALLOWED_LOCALES))}."
                )
            update_kwargs["locale"] = payload.locale
            has_changes = True

        if payload.email is not None:
            # Check email uniqueness
            existing = await self._repo.find_user_by_email(payload.email)
            if existing is not None and existing.id != user_id:
                raise ConflictError(
                    f"Email '{payload.email}' is already in use."
                )
            update_kwargs["email"] = payload.email
            has_changes = True

            # New email must be re-verified — reset flag and send OTP
            await self._repo.reset_email_verification(user_id)
            await self._otp_service.generate_and_send(
                email=payload.email,
                purpose="signup",
                locale=user.locale,
            )

        # Password change
        if payload.new_password is not None:
            if not payload.current_password:
                raise ValidationError(
                    "Current password is required to set a new password."
                )
            if user.password_hash is None:
                raise ValidationError(
                    "This account does not have a password set."
                )
            if not verify_password(payload.current_password, user.password_hash):
                raise AuthenticationError("Current password is incorrect.")
            self._validate_password(payload.new_password)
            update_kwargs["password_hash"] = hash_password(payload.new_password)
            has_changes = True

            # Send password-change notification email
            if self._email_service is not None:
                user_email = user.email or user.external_id
                if user_email:
                    from services.email_service import (  # noqa: PLC0415
                        render_email_template,
                        render_subject_template,
                        render_text_template,
                    )

                    context: dict[str, object] = {
                        "name": user.name or "there",
                    }
                    html_body = await render_email_template(
                        "password_changed", context, locale=user.locale,
                    )
                    text_body = await render_text_template(
                        "password_changed", context, locale=user.locale,
                    )
                    subject = await render_subject_template(
                        "password_changed", context, locale=user.locale,
                    )

                    try:
                        await self._email_service.send_email(
                            to=user_email,
                            subject=subject,
                            html_body=html_body,
                            text_body=text_body,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to send password-change notification email",
                            extra={"email": user_email[:3] + "**@" + user_email.split("@")[-1]},
                        )

        if has_changes:
            user = await self._repo.update_dashboard_user(
                user_id=user_id,
                **update_kwargs,
            )

        return DashboardUserResponse(
            id=user.id,
            email=user.email or "",
            name=user.name,
            role=user.role if user.role is not None else "member",
            organization_id=user.organization_id,
            is_email_verified=user.is_email_verified,
            mfa_enabled=user.mfa_enabled,
            must_change_password=bool(user.must_change_password),
            locale=user.locale,
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
