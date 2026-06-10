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

    # ── Dashboard user ──────────────────────────────────────────────────────

    async def create_dashboard_user(
        self,
        organization_id: uuid.UUID,
        email: str,
        password_hash: str,
        name: str | None = None,
        role: str = "admin",
    ) -> User:
        """Create a dashboard user (admin/member) with password auth.

        Sets ``external_id`` to the email for simplicity.  The unique
        constraint on ``(organization_id, external_id)`` prevents two
        dashboard users with the same email in the same org.

        Args:
            organization_id: Owning organization.
            email: Email address (used as external_id too).
            password_hash: bcrypt hash of the password.
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
