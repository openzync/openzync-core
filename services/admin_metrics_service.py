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
    from schemas.platform_config import PlatformConfigBase
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
        platform_config: PlatformConfigBase,
        user_id: UUID,
        start: datetime,
        end: datetime,
        sitewide: bool = False,
        project_id: UUID | None = None,
    ) -> MetricsSummaryResponse:
        """Return aggregated DB counts merged with Prometheus metrics.

        DB counts are scoped to the user — or platform-wide for
        ``platform:manage`` holders (``sitewide``, under an RLS bypass at
        the call site). Only the Prometheus
        range time-series honor the window; DB totals and instant queries
        keep snapshot semantics.

        Args:
            request: FastAPI request (graph backend dispatcher + pools).
            platform_config: Platform config (graph backend selection).
            user_id: Authenticated user ID (singleton platform).
            start: Window start.
            end: Window end.
            sitewide: When ``True`` aggregate DB counts across all users.
            project_id: Optional project filter for the DB counts.

        Returns:
            Merged metrics summary; ``status="degraded"`` with DB counts
            intact when Prometheus is unreachable.
        """
        backend = await resolve_graph_backend_fail_soft(
            request, platform_config, user_id, self._db, "admin_metrics"
        )
        episode_stats, graph_stats, user_count = await self._fetch_db_counts(
            user_id, backend, sitewide, project_id
        )
        try:
            perf = await self._prom.get_summary(
                user_id=str(user_id), start=start, end=end
            )
        except MetricsUnavailableError as exc:
            logger.warning(
                "admin_metrics.prometheus_degraded",
                user_id=str(user_id),
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
        self,
        user_id: UUID,
        backend: GraphBackend | None,
        sitewide: bool = False,
        project_id: UUID | None = None,
    ) -> tuple[EpisodeStats, GraphStats, int]:
        """Run all DB count queries for the admin summary.

        Args:
            user_id: User UUID for tenant isolation.
            backend: Resolved graph backend for entity counts (``None`` →
                zeros, degraded).
            sitewide: When ``True`` count across all users.
            project_id: Optional project filter for the episode counts.

        Returns:
            Tuple of (EpisodeStats, GraphStats, user_count).
        """
        repo = self._repo
        if sitewide:
            episodes_total = await repo.count_episodes_total_sitewide(project_id)
            episodes_24h = await repo.count_episodes_24h_sitewide(project_id)
            episodes_archived = await repo.count_episodes_archived_sitewide(project_id)
            episodes_enrichable = await repo.count_episodes_enrichable_sitewide(
                project_id
            )
            episodes_in_progress = await repo.count_episodes_in_progress_sitewide(
                project_id
            )
            episodes_pending = await repo.count_episodes_pending_sitewide(project_id)
            episodes_fully_enriched = (
                await repo.count_episodes_fully_enriched_sitewide(project_id)
            )
            episodes_with_embeddings = (
                await repo.count_episodes_with_embeddings_sitewide(project_id)
            )
        else:
            episodes_total = await repo.count_episodes_total(user_id, project_id)
            episodes_24h = await repo.count_episodes_24h(user_id, project_id)
            episodes_archived = await repo.count_episodes_archived(user_id, project_id)
            episodes_enrichable = await repo.count_episodes_enrichable(
                user_id, project_id
            )
            episodes_in_progress = await repo.count_episodes_in_progress(
                user_id, project_id
            )
            episodes_pending = await repo.count_episodes_pending(user_id, project_id)
            episodes_fully_enriched = await repo.count_episodes_fully_enriched(
                user_id, project_id
            )
            episodes_with_embeddings = await repo.count_episodes_with_embeddings(
                user_id, project_id
            )

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

        # Graph counts (backend-backed, user-scoped fan-out). entities_total is
        # inventory, so archived projects are included here.
        stats = GraphStatsService(self._db, backend)
        project_ids = await stats.resolve_project_ids(
            user_id, project_id, include_archived=True
        )
        entities_total, entities_24h = await stats.entity_totals(user_id, project_ids)
        graph_stats = GraphStats(
            entities_total=entities_total,
            entities_24h=entities_24h,
            relationships_total=0,  # GraphRelationship model TBD
        )

        users_total = await repo.count_users_total(user_id)
        return episode_stats, graph_stats, users_total
