"""Organization config service — CRUD backed by OpenBao.

This service orchestrates the config update flow:
1. Validate the update payload.
2. Delegate to ``core.org_config`` for the OpenBao update + cache invalidation.
3. Return the stored config.

Wire-up in router::

    config = await org_config_service.get_config(org_id)
    result = await org_config_service.update_config(org_id, payload)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from core.openbao import OpenBaoClient
from core.org_config import (
    get_org_config,
    update_org_config as core_update_org_config,
)
from schemas.organization_config import (
    OrgConfigBase,
    OrgConfigResponse,
    UpdateOrgConfigRequest,
)


class OrgConfigService:
    """Business logic for per-organization configuration management.

    Args:
        bao_client: An authenticated :class:`OpenBaoClient`.
        redis: An optional async Redis client (for caching).
    """

    def __init__(self, bao_client: OpenBaoClient, redis: Any | None = None) -> None:
        self._bao_client = bao_client
        self._redis = redis

    async def get_config(self, org_id: UUID) -> OrgConfigBase:
        """Return the stored config for an org.

        Args:
            org_id: The organization UUID.

        Returns:
            An ``OrgConfigBase`` with only explicitly stored fields.
            Unset fields are ``None``.
        """
        return await get_org_config(
            org_id,
            redis=self._redis,
            bao_client=self._bao_client,
        )

    async def get_config_response(self, org_id: UUID) -> OrgConfigResponse:
        """Return the stored config wrapped in an ``OrgConfigResponse``.

        Args:
            org_id: The organization UUID.

        Returns:
            An ``OrgConfigResponse`` containing the stored config.
        """
        stored = await self.get_config(org_id)
        return OrgConfigResponse(stored=stored)

    async def update_config(
        self, org_id: UUID, payload: UpdateOrgConfigRequest
    ) -> OrgConfigBase:
        """Partially update an org's configuration.

        Only the fields explicitly set in *payload* are updated.  Fields
        set to ``None`` are removed from the stored config.  The cache is
        invalidated after the update.

        Args:
            org_id: The organization UUID.
            payload: The fields to update.

        Returns:
            The freshly stored config after the update.
        """
        return await core_update_org_config(
            org_id,
            update_data=payload,
            bao_client=self._bao_client,
            redis=self._redis,
        )
