"""Admin metrics summary orchestration — service layer only, no SQL.

Merges DB counts (via :class:`AdminMetricsRepository`) with
Prometheus-backed performance metrics for the admin panel summary.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from core.exceptions import MetricsUnavailableError
from core.graph_backend import resolve_graph_backend_fail_soft
from repositories.admin_metrics_repository import AdminMetricsRepository
from schemas.admin_metrics import EpisodeStats, GraphStats, MetricsSummaryResponse
from services.graph_stats_service import GraphStatsService

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend
    from schemas.organization_config import OrgConfigBase
    from services.metrics_service import MetricsService

logger = structlog.get_logger(__name__)


class AdminMetricsService:
    """Orchestrates the admin metrics summary (DB counts + Prometheus)."""

    def __init__(self, db: AsyncSession, prom: MetricsService) -> None:
        self._db = db
        self._prom = prom
        self._repo = AdminMetricsRepository(db)

    async def get_summary(
        self,
        request: Request,
        org_config: OrgConfigBase,
        org_id: UUID,
        start: datetime,
        end: datetime,
    ) -> MetricsSummaryResponse:
        """Return aggregated DB counts merged with Prometheus metrics.

        DB counts are scoped to the organization. Only the Prometheus
        range time-series honor the window; DB totals and instant queries
        keep snapshot semantics.

        Args:
            request: FastAPI request (graph backend dispatcher + pools).
            org_config: Org config (graph backend selection).
            org_id: Authenticated organization ID.
            start: Window start.
            end: Window end.

        Returns:
            Merged metrics summary; ``status="degraded"`` with DB counts
            intact when Prometheus is unreachable.
        """
        backend = await resolve_graph_backend_fail_soft(
            request, org_config, org_id, self._db, "admin_metrics"
        )
        episode_stats, graph_stats, user_count = await self._fetch_db_counts(
            org_id, backend
        )
        try:
            perf = await self._prom.get_summary(
                org_id=str(org_id), start=start, end=end
            )
        except MetricsUnavailableError as exc:
            logger.warning(
                "admin_metrics.prometheus_degraded",
                org_id=str(org_id),
                error=str(exc),
            )
            return MetricsSummaryResponse(
                episodes=episode_stats,
                graphs=graph_stats,
                users_total=user_count,
                status="degraded",
                message=f"Prometheus unreachable: {exc}",
            )
        perf.episodes = episode_stats
        perf.graphs = graph_stats
        perf.users_total = user_count
        return perf

    async def _fetch_db_counts(
        self, org_id: UUID, backend: GraphBackend | None
    ) -> tuple[EpisodeStats, GraphStats, int]:
        """Run all DB count queries for the admin summary.

        Args:
            org_id: Organization UUID for tenant isolation.
            backend: Resolved graph backend for entity counts (``None`` →
                zeros, degraded).

        Returns:
            Tuple of (EpisodeStats, GraphStats, user_count).
        """
        repo = self._repo
        episodes_total = await repo.count_episodes_total(org_id)
        episodes_24h = await repo.count_episodes_24h(org_id)
        episodes_archived = await repo.count_episodes_archived(org_id)
        episodes_enrichable = await repo.count_episodes_enrichable(org_id)
        episodes_in_progress = await repo.count_episodes_in_progress(org_id)
        episodes_pending = await repo.count_episodes_pending(org_id)
        episodes_fully_enriched = await repo.count_episodes_fully_enriched(org_id)
        episodes_with_embeddings = await repo.count_episodes_with_embeddings(org_id)

        episode_stats = EpisodeStats(
            added_total=episodes_total,
            added_24h=episodes_24h,
            in_progress=episodes_in_progress,
            enrichment_pending=episodes_pending,
            fully_enriched=episodes_fully_enriched,
            with_embeddings=episodes_with_embeddings,
            archived_episodes=episodes_archived,
            enrichable_total=episodes_enrichable,
            fully_enriched_pct=round(
                episodes_fully_enriched / episodes_enrichable * 100, 1
            )
            if episodes_enrichable > 0
            else 0.0,
        )

        # Graph counts (backend-backed, org-wide fan-out). entities_total is
        # inventory, so archived projects are included here.
        stats = GraphStatsService(self._db, backend)
        project_ids = await stats.resolve_project_ids(
            org_id, None, include_archived=True
        )
        entities_total, entities_24h = await stats.entity_totals(org_id, project_ids)
        graph_stats = GraphStats(
            entities_total=entities_total,
            entities_24h=entities_24h,
            relationships_total=0,  # GraphRelationship model TBD
        )

        users_total = await repo.count_users_total(org_id)
        return episode_stats, graph_stats, users_total
