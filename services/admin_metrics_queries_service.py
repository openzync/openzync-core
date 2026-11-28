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
    from schemas.platform_config import PlatformConfigBase


class AdminMetricsQueriesService:
    """Facade running the predefined admin metric queries for ``/metrics/batch``."""

    def __init__(self, db: AsyncSession, prom_client: httpx.AsyncClient) -> None:
        self._batch = AdminMetricsBatchService(db=db, prom_client=prom_client)

    async def run_batch(
        self,
        request: Request,
        platform_config: PlatformConfigBase,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        include_archived: bool,
        sitewide: bool = False,
    ) -> dict:
        """Run every predefined query concurrently; partial failures stay HTTP 200.

        Args:
            request: FastAPI request (graph backend dispatcher + pools).
            platform_config: Platform config (graph backend selection).
            user_id: Tenant scope — or the holder's ID when ``sitewide``.
            start: Window start.
            end: Window end.
            limit: Max rows for rankings.
            project_id: Optional project filter.
            include_archived: Include archived projects (top projects only).
            sitewide: When ``True`` DB legs run under an RLS bypass and
                PromQL drops the user label (platform-wide aggregates).

        Returns:
            ``{"results": {name: query-dict}, "errors": [...], "meta": {...}}``
            where each ``results`` value has ``query/user_scoped/columns/rows/
            total/parameters`` and each ``errors`` entry has
            ``query/code/message``.
        """
        return await self._batch.run_batch(
            request,
            platform_config,
            user_id,
            start,
            end,
            limit,
            project_id,
            include_archived,
            sitewide,
        )
