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
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from repositories.auth_repository import AuthRepository
from schemas.auth import (
    DashboardUserResponse,
    LoginRequest,
    LoginResponse,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaVerifyRequest,
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


# ═══════════════════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════════════════


class AuthService:
    """Handles dashboard authentication flows.

    Args:
        repo: Repository for auth-related DB access.
        otp_service: OTP service for email verification and MFA.
        redis: Async Redis client for MFA session storage.
        email_service: Optional email service for notification-only emails
            (e.g. password-change confirmation).  ``None`` skips notifications.
    """

    def __init__(
        self,
        repo: AuthRepository,
        otp_service: OtpService,  # noqa: F821
        redis: AsyncRedis,  # noqa: F821
        email_service: EmailService | None = None,  # noqa: F821
    ) -> None:
        self._repo = repo
        self._otp_service = otp_service
        self._redis = redis
        self._email_service = email_service

    # ── Signup ──────────────────────────────────────────────────────────────

    async def signup(self, payload: SignupRequest) -> SignupResponse:
        """Create a new organization with an admin dashboard user.

        Flow:
        1. Check email uniqueness (no existing user with this email).
        2. Create the organization.
        3. Hash the password and create the dashboard admin user.
        4. Send an OTP verification code to the user's email.
        5. Return a confirmation message (no tokens — user must verify email).

        Args:
            payload: Signup request with email, password, org name.

        Returns:
            A ``SignupResponse`` with a confirmation message.

        Raises:
            ConflictError: If the email is already registered.
            ValidationError: If the password does not meet requirements.
        """
        # Validate password strength
        self._validate_password(payload.password)

        # Check email uniqueness
        existing = await self._repo.find_user_by_email(payload.email)
        if existing is not None:
            raise ConflictError(
                f"A user with email '{payload.email}' is already registered."
            )

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
            raise ConflictError(
                f"A user with email '{payload.email}' is already registered."
            )

        return SignupResponse(
            message="Verification code sent to email. "
            "Use POST /v1/auth/verify-email to complete signup.",
            email=payload.email,
        )

    # ── Email verification ──────────────────────────────────────────────────

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
            AuthenticationError: If the OTP is invalid or expired.
            NotFoundError: If the user does not exist.
        """
        user = await self._repo.find_user_by_email(payload.email)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

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
        return await self._issue_tokens(
            user_id=user.id,
            organization_id=user.organization_id,
            role=user.role if user.role is not None else "admin",
        )

    async def resend_verification(self, email: str) -> SignupResponse:
        """Resend the email verification OTP.

        Rate limiting is handled internally by the OtpService (cooldown
        and hourly send cap).

        Args:
            email: The email address registered during signup.

        Returns:
            A ``SignupResponse`` confirming the code was sent.

        Raises:
            NotFoundError: If no user with this email exists.
        """
        user = await self._repo.find_user_by_email(email)
        if user is None:
            raise NotFoundError(
                "No account found with this email address."
            )

        if user.is_email_verified:
            return SignupResponse(
                message="Email is already verified. You can log in.",
                email=email,
            )

        await self._otp_service.generate_and_send(
            email=email,
            purpose="signup",
        )

        return SignupResponse(
            message="Verification code resent to email.",
            email=email,
        )

    # ── Password reset ─────────────────────────────────────────────────────

    async def forgot_password(self, email: str) -> OtpResponse:
        """Send a password-reset OTP to the user's email.

        Args:
            email: The registered email address.

        Returns:
            An ``OtpResponse`` confirming the code was sent.

        Raises:
            ValidationError: If no user with this email address exists.
        """
        user = await self._repo.find_user_by_email(email)
        if user is None or user.password_hash is None:
            raise ValidationError(
                "No account found with this email address.",
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
            NotFoundError: If the user does not exist.
            AuthenticationError: If the OTP is invalid or expired.
            ValidationError: If the new password is too weak.
        """
        user = await self._repo.find_user_by_email(payload.email)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

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

        Args:
            email: The registered email address.

        Returns:
            An ``OtpResponse`` confirming the code was sent.

        Raises:
            NotFoundError: If no user with this email exists.
        """
        user = await self._repo.find_user_by_email(email)
        if user is None:
            raise NotFoundError("No account found with this email address.")

        await self._otp_service.generate_and_send(
            email=email,
            purpose="passwordless_login",
        )

        return OtpResponse(
            message="Login code sent to email. "
            "Use POST /v1/auth/login/otp/verify to complete login.",
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
            NotFoundError: If the user does not exist.
            AuthenticationError: If the OTP is invalid or expired.
        """
        user = await self._repo.find_user_by_email(payload.email)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

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

        return await self._issue_tokens(
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
        tokens = await self._issue_tokens(
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

        return await self._issue_tokens(
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

        Args:
            raw_token: The opaque refresh token string from the client.

        Returns:
            A new ``TokenResponse`` with fresh access and refresh tokens.

        Raises:
            AuthenticationError: If the refresh token is invalid or expired.
        """
        token_hash = self._hash_refresh_token(raw_token)
        stored = await self._repo.find_refresh_token(token_hash)

        if stored is None:
            raise AuthenticationError(
                "Refresh token is invalid or has expired."
            )

        # Look up the user to get the actual role
        user_id = uuid.UUID(stored.user_id)
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise AuthenticationError("User no longer exists.")
        role = user.role if user.role is not None else "member"

        # Issue new tokens first, then revoke + chain the old one
        new_tokens = await self._issue_tokens(
            user_id=user_id,
            organization_id=stored.organization_id,
            role=role,
        )

        # Find the newly created refresh token to build the rotation chain
        new_refresh_hash = self._hash_refresh_token(new_tokens.refresh_token)
        new_stored = await self._repo.find_refresh_token(new_refresh_hash)
        new_id = new_stored.id if new_stored else None

        # Revoke the old token and set rotation chain
        await self._repo.revoke_refresh_token(
            stored.id,
            rotated_by=str(new_id) if new_id else None,
        )

        return new_tokens

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _issue_tokens(
        self,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        role: str,
    ) -> TokenResponse:
        """Generate and persist an access + refresh token pair.

        Args:
            user_id: The authenticated user's UUID.
            organization_id: The user's organization UUID.
            role: User role for JWT claims.

        Returns:
            A ``TokenResponse`` with fresh tokens.
        """
        # Use naive UTC datetime for DB storage (refresh_token.expires_at
        # is TIMESTAMP WITHOUT TIME ZONE).
        now = datetime.now(UTC).replace(tzinfo=None)

        # Access token
        access_token = create_jwt_token(
            data={
                "sub": str(user_id),
                "org_id": str(organization_id),
                "role": role,
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
                        render_text_template,
                    )

                    context: dict[str, object] = {
                        "name": user.name or "there",
                    }
                    html_body = await render_email_template("password_changed", context)
                    text_body = await render_text_template("password_changed", context)

                    try:
                        await self._email_service.send_email(
                            to=user_email,
                            subject="Your OpenZync password was changed",
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
