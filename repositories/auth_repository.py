"""Auth repository — all database access for dashboard authentication.

Handles user lookup by email for login, refresh token CRUD, and
organization creation during signup.  No business logic — pure
query construction and execution.

Key patterns:
- Partial unique index on ``email WHERE email IS NOT NULL`` for
  login lookup.
- Refresh tokens use a rotation chain (``rotated_by`` FK) for audit
  and revocation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.organization import Organization
from models.refresh_token import RefreshToken
from models.user import User


class AuthRepository:
    """All database access for authentication flows.

    Every public method works within a single organization scope
    except ``find_user_by_email`` which scans globally (email is
    globally unique).
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── User lookup ─────────────────────────────────────────────────────────

    async def find_user_by_email(self, email: str) -> User | None:
        """Find a dashboard user by email (global lookup).

        Uses the partial unique index on ``email`` — email is globally
        unique across all organizations.

        Args:
            email: The user's email address.

        Returns:
            The User with ``password_hash`` set, or ``None``.
        """
        result = await self._db.execute(
            select(User).where(
                User.email == email,
                User.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def get_user_by_id(self, user_id: uuid.UUID) -> User | None:
        """Get a user by UUID (no org scope — used during token validation).

        Args:
            user_id: The internal user UUID.

        Returns:
            The User if found, or ``None``.
        """
        result = await self._db.execute(
            select(User).where(
                User.id == user_id,
                User.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    # ── Organization ────────────────────────────────────────────────────────

    async def create_organization(
        self, name: str, plan: str = "free"
    ) -> Organization:
        """Create a new organization.

        Args:
            name: Organization name.
            plan: Billing plan (default ``'free'``).

        Returns:
            The newly created Organization.
        """
        org = Organization(name=name, plan=plan)
        self._db.add(org)
        await self._db.flush()
        await self._db.refresh(org)
        return org

    async def seed_prompts_for_org(self, org_id: uuid.UUID) -> int:
        """Seed prompt templates from the disk manifest into a new org.

        Delegates to :class:`PromptTemplateRepository.seed_default_prompts`
        which reads ``services/worker/prompts/manifest.yaml`` + ``.jinja2`` files.

        Args:
            org_id: UUID of the newly created organisation.

        Returns:
            Number of templates seeded.
        """
        from repositories.prompt_template_repository import (
            PromptTemplateRepository,
        )

        return await PromptTemplateRepository(self._db).seed_default_prompts(org_id)

    # ── Dashboard user ──────────────────────────────────────────────────────

    async def create_dashboard_user(
        self,
        organization_id: uuid.UUID,
        email: str,
        password_hash: str | None,
        name: str | None = None,
        role: str = "admin",
    ) -> User:
        """Create a dashboard user (admin/member).

        Supports both password-authenticated users (email/password) and
        OAuth-authenticated users (no password — pass ``None``).

        Sets ``external_id`` to the email for simplicity.  The unique
        constraint on ``(organization_id, external_id)`` prevents two
        dashboard users with the same email in the same org.

        Args:
            organization_id: Owning organization.
            email: Email address (used as external_id too).
            password_hash: bcrypt hash of the password, or ``None`` for
                OAuth-authenticated users.
            name: Optional display name.
            role: Role string (``'admin'`` or ``'member'``).

        Returns:
            The newly created User.
        """
        user = User(
            organization_id=organization_id,
            external_id=email,
            email=email,
            name=name,
            password_hash=password_hash,
            role=role,
            metadata_={},
        )
        self._db.add(user)
        await self._db.flush()
        await self._db.refresh(user)
        return user

    # ── Refresh tokens ─────────────────────────────────────────────────────

    async def create_refresh_token(
        self,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
    ) -> RefreshToken:
        """Create a new refresh token record.

        Args:
            user_id: The authenticated user's UUID.
            organization_id: The user's organization UUID.
            token_hash: SHA-256 hash of the opaque refresh token string.
            expires_at: Expiration timestamp (naive UTC).

        Returns:
            The newly created RefreshToken.
        """
        rt = RefreshToken(
            user_id=str(user_id),
            organization_id=organization_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._db.add(rt)
        await self._db.flush()
        await self._db.refresh(rt)
        return rt

    async def find_refresh_token(
        self, token_hash: str
    ) -> RefreshToken | None:
        """Look up a refresh token by its hash.

        Args:
            token_hash: SHA-256 hex digest of the raw token.

        Returns:
            The RefreshToken if found, or ``None``.
        """
        result = await self._db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.is_revoked.is_(False),
                RefreshToken.expires_at > datetime.utcnow(),
            )
        )
        return result.scalar_one_or_none()

    async def revoke_refresh_token(
        self,
        token_id: uuid.UUID,
        rotated_by: str | None = None,
    ) -> None:
        """Revoke a refresh token, optionally setting the rotation chain.

        Args:
            token_id: UUID of the token to revoke.
            rotated_by: UUID of the new token that replaces this one
                (rotation chain for audit).
        """
        result = await self._db.execute(
            select(RefreshToken).where(RefreshToken.id == token_id)
        )
        rt = result.scalar_one_or_none()
        if rt is not None:
            rt.is_revoked = True
            if rotated_by is not None:
                rt.rotated_by = uuid.UUID(rotated_by)
            await self._db.flush()

    async def revoke_all_refresh_tokens(self, user_id: uuid.UUID) -> None:
        """Revoke all active refresh tokens for a given user.

        Called after a password reset to invalidate all existing sessions —
        the user must re-authenticate with the new password.

        Args:
            user_id: The user's UUID.
        """
        from sqlalchemy import update

        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.user_id == str(user_id),
                RefreshToken.is_revoked.is_(False),
            )
            .values(is_revoked=True)
        )
        await self._db.execute(stmt)
        await self._db.flush()

    # ── Email verification ─────────────────────────────────────────────────

    async def mark_email_verified(self, user_id: uuid.UUID) -> User:
        """Mark a user's email as verified and record the timestamp.

        Args:
            user_id: The user's UUID.

        Returns:
            The updated User instance.

        Raises:
            NotFoundError: If the user does not exist.
        """
        from core.exceptions import NotFoundError

        user = await self.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        user.is_email_verified = True
        user.email_verified_at = datetime.utcnow()
        await self._db.flush()
        await self._db.refresh(user)
        return user

    async def reset_email_verification(self, user_id: uuid.UUID) -> User:
        """Reset a user's email verification status (e.g. after email change).

        Args:
            user_id: The user's UUID.

        Returns:
            The updated User instance.

        Raises:
            NotFoundError: If the user does not exist.
        """
        from core.exceptions import NotFoundError

        user = await self.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        user.is_email_verified = False
        user.email_verified_at = None
        await self._db.flush()
        await self._db.refresh(user)
        return user

    # ── MFA ────────────────────────────────────────────────────────────────

    async def set_mfa_enabled(self, user_id: uuid.UUID, enabled: bool) -> User:
        """Enable or disable MFA for a dashboard user.

        Args:
            user_id: The user's UUID.
            enabled: ``True`` to enable MFA, ``False`` to disable.

        Returns:
            The updated User instance.

        Raises:
            NotFoundError: If the user does not exist.
        """
        from core.exceptions import NotFoundError

        user = await self.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        user.mfa_enabled = enabled
        await self._db.flush()
        await self._db.refresh(user)
        return user

    # ── Session helpers (used by service layer for ORM mutations) ─────────

    async def flush(self) -> None:
        """Flush pending ORM changes to the database.

        Used after direct attribute mutations on ORM instances in the
        service layer, avoiding access to the private ``_db`` attribute.
        """
        await self._db.flush()

    async def refresh(self, instance: Any) -> None:
        """Refresh an ORM instance from the database.

        Args:
            instance: The ORM instance to refresh.
        """
        await self._db.refresh(instance)

    async def update_dashboard_user(
        self,
        user_id: uuid.UUID,
        name: str | None = None,
        email: str | None = None,
        password_hash: str | None = None,
    ) -> User:
        """Update a dashboard user's profile fields and flush.

        Encapsulates ORM attribute mutations so the service layer does
        not need to import or manipulate ORM objects directly.

        Args:
            user_id: The user's UUID.
            name: New display name (``None`` = no change).
            email: New email (``None`` = no change).  When set,
                ``external_id`` is also updated to match.
            password_hash: New bcrypt hash (``None`` = no change).

        Returns:
            The updated User instance.

        Raises:
            NotFoundError: If the user does not exist (handled by caller).
        """
        from core.exceptions import NotFoundError

        user = await self.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        if name is not None:
            user.name = name
        if email is not None:
            user.email = email
            user.external_id = email
        if password_hash is not None:
            user.password_hash = password_hash

        await self._db.flush()
        await self._db.refresh(user)
        return user
