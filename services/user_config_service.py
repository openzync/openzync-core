"""User config service — per-user overrides over platform config.

Resolution order: ``user override ⊕ org config ⊕ system defaults ⊕
OpenBao``, later layers only filling keys the earlier ones leave unset.
System and org layers come from OpenBao; the user layer is the
``users.config_override`` JSONB column.  No secrets are logged.
"""

from __future__ import annotations

from typing import Any
from typing import Any
from uuid import UUID

from repositories.user_repository import UserRepository


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` overlaid with ``overlay`` (nested dicts merge).

    Args:
        base: Lower-precedence mapping.
        overlay: Higher-precedence mapping (wins on conflict).

    Returns:
        A new merged dict — neither input is mutated.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class UserConfigService:
    """Resolves and mutates per-user config overrides.

    Args:
        user_repo: Repository for ``users.config_override`` access.
        bao_client: Authenticated OpenBao client (org/system layers).
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

        Args:
            user_id: The internal OpenZync user UUID.

        Returns:
            The merged effective config dict actually applied.

        Raises:
            NotFoundError: If the user does not exist.
        """
        from core.exceptions import NotFoundError

        from repositories.auth_repository import AuthRepository

        user = await AuthRepository(self._user_repo.session).get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Dashboard user not found.")

        effective: dict[str, Any] = {}
        if self._bao_client is not None:
            from core.org_config import get_org_config
            from core.system_config import get_system_config

            system = await get_system_config(self._redis, self._bao_client)
            effective = _deep_merge(
                effective,
                system.model_dump(exclude_none=True),
            )
            try:
                org = await get_org_config(
                    user.organization_id,
                    redis=self._redis,
                    bao_client=self._bao_client,
                )
                effective = _deep_merge(
                    effective, org.model_dump(exclude_none=True)
                )
            except Exception:
                pass  # org layer optional — user still gets system + override

        override = await self._user_repo.get_config_override(user_id)
        return _deep_merge(effective, override)
