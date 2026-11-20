"""User config service — per-user overrides over platform config.

Resolution order: ``user override ⊕ system defaults ⊕ stored org config``,
later layers only filling keys the earlier ones leave unset.  The system
layer comes from the platform root row; the org layer from the user's own
``users.config_override`` row; the user layer is the same row's
``config_override`` (kept as a separate accessor for the dashboard
override UI).  No secrets are logged.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.dict_merge import deep_merge
from core.exceptions import NotFoundError
from core.org_config import get_org_config
from core.system_config import get_system_config
from repositories.auth_repository import AuthRepository
from repositories.user_repository import UserRepository


class UserConfigService:
    """Resolves and mutates per-user config overrides.

    Args:
        user_repo: Repository for ``users.config_override`` access.
        redis: Optional async Redis client (org/system cache).
        auth_repo: Injected auth repository (shares the request session).
        db: Async DB session for org/system config reads.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        redis: Any | None = None,
        auth_repo: AuthRepository | None = None,
        db: AsyncSession | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._redis = redis
        # Injected (not constructed inline) so callers share the request
        # session and tests can substitute a fake.
        self._auth_repo = auth_repo or AuthRepository(user_repo.session)
        self._db = db or user_repo.session

    async def get_override(self, user_id: UUID) -> dict[str, Any]:
        """Return the user's stored override (``{}`` when none).

        Args:
            user_id: The internal OpenZync user UUID.

        Returns:
            The override dict.
        """
        return await self._user_repo.get_config_override(user_id)

    async def update_override(
        self, user_id: UUID, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Deep-merge ``patch`` onto the user's override (``None`` removes a top-level key).

        Nested dicts merge recursively; removal is top-level only by
        design (a ``None`` nested value removes that nested key's parent
        entry at the top level, never a deep path).

        Args:
            user_id: The internal OpenZync user UUID.
            patch: Partial keys to merge; ``None`` values remove keys.

        Returns:
            The updated override dict.
        """
        return await self._user_repo.merge_config_override(user_id, patch)

    async def get_effective_config(self, user_id: UUID) -> dict[str, Any]:
        """Resolve ``user ⊕ system ⊕ org`` for a dashboard user.

        Every layer failure propagates — a silently dropped user layer
        would apply the wrong config, so the error is loud, not logged
        and swallowed.

        Args:
            user_id: The internal OpenZync user UUID.

        Returns:
            The merged effective-config dict actually applied.

        Raises:
            NotFoundError: If the user does not exist.
        """
        user = await self._auth_repo.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        # System-wide defaults live on the platform root row; per-user
        # settings under ``users.config_override`` keyed by the user's own
        # UUID.  No env-var fallback — unset keys stay ``None`` and callers
        # decide.
        system = await get_system_config(self._redis, self._db)
        effective: dict[str, Any] = deep_merge(
            {}, system.model_dump(exclude_none=True)
        )
        org = await get_org_config(
            user.id,
            redis=self._redis,
            db=self._db,
        )
        effective = deep_merge(effective, org.model_dump(exclude_none=True))

        override = await self._user_repo.get_config_override(user_id)
        return deep_merge(effective, override)
