"""Admin metrics predefined batch queries — thin facade, no logic.

Per-query implementations live in ``services/admin_metrics/`` by
sub-domain (``db_queries_service``, ``prom_queries_service``,
``batch_service``); this module preserves the router import path and
delegates to :class:`AdminMetricsBatchService`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from services.admin_metrics.batch_service import AdminMetricsBatchService

if TYPE_CHECKING:
    from schemas.organization_config import OrgConfigBase


class AdminMetricsQueriesService:
    """Facade running the predefined admin metric queries for ``/metrics/batch``."""

    def __init__(self, db: AsyncSession, prom_client: httpx.AsyncClient) -> None:
        self._batch = AdminMetricsBatchService(db=db, prom_client=prom_client)

    async def run_batch(
        self,
        request: Request,
        org_config: OrgConfigBase,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        include_archived: bool,
    ) -> dict:
        """Run every predefined query concurrently; partial failures stay HTTP 200.

        Returns:
            ``{"results": {name: query-dict}, "errors": [...], "meta": {...}}``
            where each ``results`` value has ``query/org_scoped/columns/rows/
            total/parameters`` and each ``errors`` entry has
            ``query/code/message``.
        """
        return await self._batch.run_batch(
            request,
            org_config,
            org_id,
            start,
            end,
            limit,
            project_id,
            include_archived,
        )
