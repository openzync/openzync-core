"""OAuth repository — database access for OAuth account linking.

Handles CRUD for OAuthAccount records which map external OAuth provider
identities to dashboard users. No business logic — pure query construction
and execution.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.oauth_account import OAuthAccount


class OAuthRepository:
    """All database access for OAuth account records.

    Every public method works with a scoped session passed at init time.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def find_by_provider(
        self,
        provider: str,
        provider_user_id: str,
    ) -> OAuthAccount | None:
        """Find an OAuth link by provider and provider user ID.

        Uses the unique constraint on (provider, provider_user_id).

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).
            provider_user_id: The user's ID from the OAuth provider.

        Returns:
            The OAuthAccount if found, or ``None``.
        """
        result = await self._db.execute(
            select(OAuthAccount).where(
                OAuthAccount.provider == provider,
                OAuthAccount.provider_user_id == provider_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def find_by_user_id(
        self,
        user_id: uuid.UUID,
    ) -> Sequence[OAuthAccount]:
        """Get all OAuth accounts linked to a dashboard user.

        Args:
            user_id: The dashboard user's UUID.

        Returns:
            A list of OAuthAccount records (may be empty).
        """
        result = await self._db.execute(
            select(OAuthAccount).where(
                OAuthAccount.user_id == user_id,
            )
        )
        return result.scalars().all()

    async def create(
        self,
        provider: str,
        provider_user_id: str,
        user_id: uuid.UUID,
    ) -> OAuthAccount:
        """Link an OAuth provider identity to a dashboard user.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).
            provider_user_id: The user's ID from the OAuth provider.
            user_id: The dashboard user's UUID.

        Returns:
            The newly created OAuthAccount.

        Raises:
            sqlalchemy.exc.IntegrityError: If the (provider, provider_user_id)
                combination already exists (duplicate link).
        """
        account = OAuthAccount(
            provider=provider,
            provider_user_id=provider_user_id,
            user_id=user_id,
        )
        self._db.add(account)
        await self._db.flush()
        await self._db.refresh(account)
        return account

    async def delete(self, account_id: uuid.UUID) -> None:
        """Remove an OAuth account link.

        Args:
            account_id: The OAuthAccount UUID to remove.
        """
        result = await self._db.execute(
            select(OAuthAccount).where(OAuthAccount.id == account_id)
        )
        account = result.scalar_one_or_none()
        if account is not None:
            await self._db.delete(account)
            await self._db.flush()
