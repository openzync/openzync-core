"""User config service — per-user overrides over platform config.

Resolution order: ``user override ⊕ org config ⊕ system defaults ⊕
OpenBao``, later layers only filling keys the earlier ones leave unset.
System and org layers come from OpenBao; the user layer is the
``users.config_override`` JSONB column.  No secrets are logged.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.dict_merge import deep_merge
from core.exceptions import NotFoundError
from core.openbao_exceptions import OpenBaoConnectionError
from core.org_config import get_org_config
from core.system_config import get_system_config
from repositories.auth_repository import AuthRepository
from repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


class UserConfigService:
    """Resolves and mutates per-user config overrides.

    Args:
        user_repo: Repository for ``users.config_override`` access.
        bao_client: Authenticated OpenBao client (org/system layers).
            Required — without it the org/system layers cannot resolve.
        redis: Optional async Redis client (org/system cache).
    """

    def __init__(
        self,
        user_repo: UserRepository,
        bao_client: Any | None = None,
        redis: Any | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._bao_client = bao_client
        self._redis = redis

    def _bao(self) -> Any:
        """Return the OpenBao client or fail loud (no silent degraded config)."""
        if self._bao_client is None:
            raise OpenBaoConnectionError(
                "OpenBao client required for user effective-config reads"
            )
        return self._bao_client

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
        """Deep-merge ``patch`` onto the user's override (``None`` removes).

        Args:
            user_id: The internal OpenZync user UUID.
            patch: Partial keys to merge; ``None`` values remove keys.

        Returns:
            The updated override dict.
        """
        return await self._user_repo.merge_config_override(user_id, patch)

    async def get_effective_config(self, user_id: UUID) -> dict[str, Any]:
        """Resolve ``user ⊕ org ⊕ system`` for a dashboard user.

        Every layer failure propagates — a silently dropped org layer
        would apply the wrong config, so the error is loud, not logged
        and swallowed.

        Args:
            user_id: The internal OpenZync user UUID.

        Returns:
            The merged effective config dict actually applied.

        Raises:
            NotFoundError: If the user does not exist.
            OpenBaoConnectionError: If no OpenBao client is wired.
        """
        user = await AuthRepository(self._user_repo.session).get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        bao = self._bao()
        system = await get_system_config(self._redis, bao)
        effective: dict[str, Any] = deep_merge(
            {}, system.model_dump(exclude_none=True)
        )
        try:
            org = await get_org_config(
                user.organization_id,
                redis=self._redis,
                bao_client=bao,
            )
        except Exception:
            logger.error(
                "user_config.org_layer_failed",
                extra={"user_id": str(user_id)},
                exc_info=True,
            )
            raise
        effective = deep_merge(effective, org.model_dump(exclude_none=True))

        override = await self._user_repo.get_config_override(user_id)
        return deep_merge(effective, override)
