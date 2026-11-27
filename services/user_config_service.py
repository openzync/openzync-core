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

from core.config import get_settings
from core.dict_merge import deep_merge
from core.exceptions import NotFoundError, ValidationError
from core.org_config import get_platform_config
from core.system_config import get_system_config
from repositories.auth_repository import AuthRepository
from repositories.user_repository import UserRepository
from schemas.organization_config import (
    SYSTEM_MANAGED_FALKORDB_FIELDS,
    SYSTEM_MANAGED_SURREALDB_FIELDS,
)


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

    def get_system_managed_fields(self) -> list[str]:
        """Return field names locked at the system level.

        Returns:
            Managed field names (e.g. ``["falkordb_url"]`` when
            system-level FalkorDB URL is set, else ``[]``).
        """
        settings = get_settings()
        fields: list[str] = []
        if settings.SURREALDB_URL:
            fields.extend(SYSTEM_MANAGED_SURREALDB_FIELDS)
        if settings.FALKORDB_URL:
            fields.extend(SYSTEM_MANAGED_FALKORDB_FIELDS)
        return fields

    async def update_override(
        self, user_id: UUID, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Deep-merge ``patch`` onto the user's override (``None`` removes a key).

        Nested dicts merge recursively; a nested ``None`` removes that
        nested key (e.g. ``{"enrichment": {"facts": None}}`` resets just
        the ``facts`` toggle, restoring its default ``True``).

        Args:
            user_id: The internal OpenZync user UUID.
            patch: Partial keys to merge; ``None`` values remove keys.

        Returns:
            The updated override dict.

        Raises:
            ValidationError: If *patch* touches system-managed fields.
        """
        managed = set(self.get_system_managed_fields())
        if managed:
            overridden = managed.intersection(patch)
            if overridden:
                raise ValidationError(
                    "These fields are configured at the system level "
                    "and cannot be modified: "
                    f"{', '.join(sorted(overridden))}."
                )
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
        org = await get_platform_config(
            user.id,
            redis=self._redis,
            db=self._db,
        )
        effective = deep_merge(effective, org.model_dump(exclude_none=True))

        override = await self._user_repo.get_config_override(user_id)
        return deep_merge(effective, override)
