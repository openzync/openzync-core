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
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.exceptions import AuthenticationError, ConflictError, NotFoundError, ValidationError
from repositories.auth_repository import AuthRepository
from schemas.auth import (
    DashboardUserResponse,
    LoginRequest,
    SignupRequest,
    TokenResponse,
    UpdateProfileRequest,
)
from utils.crypto import create_jwt_token
from utils.password import hash_password, verify_password

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

ACCESS_TOKEN_TTL = timedelta(minutes=settings.JWT_ACCESS_TOKEN_TTL_MINUTES)
REFRESH_TOKEN_TTL = timedelta(days=settings.JWT_REFRESH_TOKEN_TTL_DAYS)

_JWT_ALGORITHM = "HS256"


# ═══════════════════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════════════════


class AuthService:
    """Handles dashboard authentication flows.

    Args:
        repo: Repository for auth-related DB access.
    """

    def __init__(self, repo: AuthRepository) -> None:
        self._repo = repo

    # ── Signup ──────────────────────────────────────────────────────────────

    async def signup(self, payload: SignupRequest) -> TokenResponse:
        """Create a new organization with an admin dashboard user.

        Flow:
        1. Check email uniqueness (no existing user with this email).
        2. Create the organization.
        3. Hash the password and create the dashboard admin user.
        4. Generate and persist a refresh token.
        5. Return access + refresh token pair.

        Args:
            payload: Signup request with email, password, org name.

        Returns:
            A ``TokenResponse`` with access and refresh tokens.

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
        org = await self._repo.create_organization(
            name=payload.organization_name,
            plan="free",
        )

        # Seed default prompt templates for the new org
        await self._repo.seed_prompts_for_org(org.id)

        # Create dashboard admin user
        pw_hash = hash_password(payload.password)
        user = await self._repo.create_dashboard_user(
            organization_id=org.id,
            email=payload.email,
            password_hash=pw_hash,
            name=payload.email.split("@")[0],  # default name from email
            role="admin",
        )

        # Generate tokens
        return await self._issue_tokens(
            user_id=user.id,
            organization_id=org.id,
            role=user.role if user.role is not None else "admin",
        )

    # ── Login ───────────────────────────────────────────────────────────────

    async def login(self, payload: LoginRequest) -> TokenResponse:
        """Authenticate a dashboard user and return tokens.

        Args:
            payload: Login request with email and password.

        Returns:
            A ``TokenResponse`` with access and refresh tokens.

        Raises:
            AuthenticationError: If email not found or password wrong.
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

        return await self._issue_tokens(
            user_id=user.id,
            organization_id=user.organization_id,
            role=user.role if user.role is not None else "member",
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
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Access token
        access_token = create_jwt_token(
            data={
                "sub": str(user_id),
                "org_id": str(organization_id),
                "role": role,
                "type": "access",
            },
            secret=settings.SECRET_KEY,
            expires_delta=ACCESS_TOKEN_TTL,
        )

        # Refresh token (opaque — stored as SHA-256 hash)
        raw_refresh = secrets.token_hex(32)
        refresh_hash = self._hash_refresh_token(raw_refresh)
        refresh_expires = now + REFRESH_TOKEN_TTL

        await self._repo.create_refresh_token(
            user_id=user_id,
            organization_id=organization_id,
            token_hash=refresh_hash,
            expires_at=refresh_expires,
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=raw_refresh,
            expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
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
        # Additional checks (uppercase, digit, etc.) can be added here.
