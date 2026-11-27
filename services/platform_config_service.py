"""Platform config service — CRUD backed by the DB.

This service orchestrates the config update flow:
1. Validate the update payload.
2. Delegate to ``core.platform_config`` for the DB update + cache invalidation.
3. Return the stored config.

Wire-up in router::

    config = await platform_config_service.get_config(user_id)
    result = await platform_config_service.update_config(user_id, payload)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.platform_config import get_platform_config
from core.platform_config import (
    update_platform_config as core_update_platform_config,
)
from schemas.platform_config import (
    PlatformConfigBase,
    PlatformConfigResponse,
    UpdatePlatformConfigRequest,
)


class PlatformConfigService:
    """Business logic for per-user platform configuration management.

    Args:
        db: Async DB session for ``users.config_override`` access.
        redis: An optional async Redis client (for caching).
    """

    def __init__(self, db: AsyncSession, redis: Any | None = None) -> None:
        self._db = db
        self._redis = redis

    async def get_config(self, user_id: UUID) -> PlatformConfigBase:
        """Return the stored config for a platform.

        Args:
            user_id: The owner user UUID.

        Returns:
            An ``PlatformConfigBase`` with only explicitly stored fields.
            Unset fields are ``None``.
        """
        return await get_platform_config(
            user_id,
            redis=self._redis,
            db=self._db,
        )

    async def get_config_response(self, user_id: UUID) -> PlatformConfigResponse:
        """Return the stored config wrapped in an ``PlatformConfigResponse``.

        Args:
            user_id: The owner user UUID.

        Returns:
            An ``PlatformConfigResponse`` containing the stored config.
        """
        stored = await self.get_config(user_id)
        return PlatformConfigResponse(stored=stored)

    async def update_config(
        self, user_id: UUID, payload: UpdatePlatformConfigRequest
    ) -> PlatformConfigBase:
        """Partially update an user's configuration.

        Only the fields explicitly set in *payload* are updated.  Fields
        set to ``None`` are removed from the stored config.  The cache is
        invalidated after the update.

        Args:
            user_id: The owner user UUID.
            payload: The fields to update.

        Returns:
            The freshly stored config after the update.
        """
        return await core_update_platform_config(
            user_id,
            update_data=payload,
            redis=self._redis,
            db=self._db,
        )
