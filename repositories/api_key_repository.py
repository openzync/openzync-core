"""API Key repository — all database access for API key management.

Handles listing, creating, and revoking API keys scoped to an organization.
No business logic — pure query construction and execution.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from models.api_key import ApiKey


class ApiKeyRepository:
    """All database access for API keys, scoped to an organization."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_by_org(
        self,
        organization_id: uuid.UUID,
        include_revoked: bool = False,
        project_id: uuid.UUID | None = None,
    ) -> Sequence[ApiKey]:
        """List API keys for an organization, optionally filtered by project.

        Args:
            organization_id: Tenant scope.
            include_revoked: If ``True``, include revoked keys.
            project_id: Optional project scope — when provided, only keys
                scoped to this project (or org-wide with NULL project_id)
                are returned.

        Returns:
            All matching ApiKey records, ordered by creation date (newest first).
        """
        query = select(ApiKey).where(
            ApiKey.organization_id == organization_id,
        )
        if project_id is not None:
            query = query.where(
                (ApiKey.project_id == project_id) | (ApiKey.project_id.is_(None))
            )
        if not include_revoked:
            query = query.where(ApiKey.is_revoked.is_(False))
        query = query.order_by(ApiKey.created_at.desc())

        result = await self._db.execute(query)
        return result.scalars().all()

    async def get_by_id(
        self,
        organization_id: uuid.UUID,
        key_id: uuid.UUID,
        project_id: uuid.UUID | None = None,
    ) -> ApiKey | None:
        """Get a single API key by ID, scoped to the organization and optionally project.

        Args:
            organization_id: Tenant scope.
            key_id: The API key UUID.
            project_id: Optional project scope for additional filtering.

        Returns:
            The ApiKey if found, or ``None``.
        """
        query = select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.organization_id == organization_id,
        )
        if project_id is not None:
            query = query.where(
                (ApiKey.project_id == project_id) | (ApiKey.project_id.is_(None))
            )
        result = await self._db.execute(query)
        return result.scalar_one_or_none()

    async def create(
        self,
        organization_id: uuid.UUID,
        lookup_hash: str,
        key_hash: str,
        salt: str,
        prefix: str,
        name: str,
        scopes: list[str] | None = None,
        project_id: uuid.UUID | None = None,
    ) -> ApiKey:
        """Create a new API key record.

        Args:
            organization_id: Owning organization.
            lookup_hash: Unsalted SHA-256 of the raw key (for fast lookup).
            key_hash: Salted SHA-256 hash of the raw key (for verification).
            salt: Hex-encoded 16-byte salt.
            prefix: Key prefix (``mg_live_`` or ``mg_test_``).
            name: Human-readable label.
            scopes: Permission scopes (defaults to ``["read", "write"]``).
            project_id: Optional project scope. ``None`` means org-wide key.

        Returns:
            The newly created ApiKey.
        """
        api_key = ApiKey(
            organization_id=organization_id,
            project_id=project_id,
            lookup_hash=lookup_hash,
            key_hash=key_hash,
            salt=salt,
            prefix=prefix,
            name=name,
            scopes=scopes or ["read", "write"],
        )
        self._db.add(api_key)
        await self._db.flush()
        await self._db.refresh(api_key)
        return api_key

    async def revoke(
        self, organization_id: uuid.UUID, key_id: uuid.UUID
    ) -> ApiKey | None:
        """Revoke an API key (soft delete).

        Args:
            organization_id: Tenant scope.
            key_id: The API key UUID to revoke.

        Returns:
            The revoked ApiKey, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(ApiKey).where(
                ApiKey.id == key_id,
                ApiKey.organization_id == organization_id,
            )
        )
        api_key = result.scalar_one_or_none()
        if api_key is None:
            return None
        api_key.is_revoked = True
        await self._db.flush()
        await self._db.refresh(api_key)
        return api_key

    async def get_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None:
        """Get an API key by its lookup hash.

        Used during API key authentication — the incoming key is hashed with
        SHA-256 and matched against ``lookup_hash`` for a fast, constant-time
        candidate lookup. The full hash verification (with salt) happens in
        the service layer.

        Args:
            lookup_hash: Unsalted SHA-256 hex digest of the API key prefix.

        Returns:
            The ApiKey if found and not soft-deleted, or ``None``.
        """
        result = await self._db.execute(
            select(ApiKey).where(
                ApiKey.lookup_hash == lookup_hash,
                ApiKey.is_revoked.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def update_last_used(self, key_id: uuid.UUID) -> None:
        """Update the ``last_used_at`` timestamp for an API key.

        Called after every successful API key authentication to track key
        usage for audit and rotation decisions.

        Args:
            key_id: The API key UUID to update.
        """
        await self._db.execute(
            sa_update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(last_used_at=func.now())
        )
        await self._db.flush()
