"""Admin stats orchestration — service layer only, no SQL.

Shapes repository rows into dashboard responses and resolves the
graph backend (fail-soft to zeros) for node counts.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from core.graph_backend import resolve_graph_backend_fail_soft
from repositories.admin_stats_repository import AdminStatsRepository
from schemas.admin_stats import OrgStatsResponse, UsageStatsResponse
from services.graph_stats_service import GraphStatsService


class AdminStatsService:
    """Orchestrates user/usage stats for the admin dashboard."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = AdminStatsRepository(db)

    async def get_platform_stats(
        self,
        user_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> OrgStatsResponse:
        """Return windowed aggregate counts for the platform.

        Args:
            user_id: Authenticated user ID.
            start: Window start (inclusive).
            end_exclusive: Window end (exclusive), or ``None`` for open-ended.
            project_id: Optional project scope.
            sitewide: When ``True`` aggregate across all users (the caller
                runs under an RLS bypass); otherwise caller-scoped.

        Returns:
            OrgStatsResponse with windowed aggregate counts.
        """
        repo = self._repo
        if sitewide:
            return OrgStatsResponse(
                user_id=user_id,
                total_episodes=await repo.count_episodes_sitewide(
                    start, end_exclusive, project_id
                ),
                total_sessions=await repo.count_sessions_sitewide(
                    start, end_exclusive, project_id
                ),
                total_facts=await repo.count_facts_sitewide(
                    start, end_exclusive, project_id
                ),
                total_extractions=await repo.count_extractions_sitewide(
                    start, end_exclusive, project_id
                ),
                total_observations=await repo.count_observations_sitewide(
                    start, end_exclusive, project_id
                ),
                total_classifications=await repo.count_classifications_sitewide(
                    start, end_exclusive, project_id
                ),
            )
        return OrgStatsResponse(
            user_id=user_id,

            total_episodes=await self._repo.count_episodes(
                user_id, start, end_exclusive, project_id
            ),
            total_sessions=await self._repo.count_sessions(
                user_id, start, end_exclusive, project_id
            ),
            total_facts=await self._repo.count_facts(
                user_id, start, end_exclusive, project_id
            ),
            total_extractions=await self._repo.count_extractions(
                user_id, start, end_exclusive, project_id
            ),
            total_observations=await self._repo.count_observations(
                user_id, start, end_exclusive, project_id
            ),
            total_classifications=await self._repo.count_classifications(
                user_id, start, end_exclusive, project_id
            ),
        )

    async def get_usage_stats(
        self,
        request: Request,
        user_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> list[UsageStatsResponse]:
        """Return daily usage counts, newest first.

        Resolves the user's stored config internally (for graph backend
        selection) from app-state Redis/DB — no extra caller wiring.

        Args:
            request: FastAPI request (graph backend dispatcher + pools,
                app-state Redis for user-config resolution).
            user_id: Authenticated user ID.
            start: Window start (inclusive).
            end_exclusive: Window end (exclusive), or ``None`` for open-ended.
            project_id: Optional project scope.
            sitewide: When ``True`` aggregate across all users (the caller
                runs under an RLS bypass — the project fan-out below then
                sees every project; graph counts stay best-effort on the
                holder's backend).

        Returns:
            List of daily usage data points, newest first.
        """
        daily_counts: dict[str, dict[str, int]] = {}

        from core.platform_config import get_platform_config as _get_platform_config

        redis = getattr(request.app.state, "redis", None)
        user_config = await _get_platform_config(
            user_id, redis=redis, db=self._db
        )

        def _ensure(date_str: str) -> dict[str, int]:
            return daily_counts.setdefault(
                date_str,
                {
                    "episode_count": 0,
                    "session_count": 0,
                    "fact_count": 0,
                    "extraction_count": 0,
                    "observation_count": 0,
                    "classification_count": 0,
                    "node_count": 0,
                    "edge_count": 0,
                },
            )

        def _merge(rows: list[tuple[datetime, int]], key: str) -> None:
            for day, count in rows:
                date_str = str(day.date()) if hasattr(day, "date") else str(day)
                _ensure(date_str)[key] = count

        if sitewide:
            _merge(
                await self._repo.episodes_sitewide_per_day(
                    start, end_exclusive, project_id
                ),
                "episode_count",
            )
            _merge(
                await self._repo.sessions_sitewide_per_day(
                    start, end_exclusive, project_id
                ),
                "session_count",
            )
            _merge(
                await self._repo.facts_sitewide_per_day(
                    start, end_exclusive, project_id
                ),
                "fact_count",
            )
            _merge(
                await self._repo.extractions_sitewide_per_day(
                    start, end_exclusive, project_id
                ),
                "extraction_count",
            )
            _merge(
                await self._repo.observations_sitewide_per_day(
                    start, end_exclusive, project_id
                ),
                "observation_count",
            )
            _merge(
                await self._repo.classifications_sitewide_per_day(
                    start, end_exclusive, project_id
                ),
                "classification_count",
            )
        else:
            _merge(
                await self._repo.episodes_per_day(user_id, start, end_exclusive, project_id),
                "episode_count",
            )
            _merge(
                await self._repo.sessions_per_day(user_id, start, end_exclusive, project_id),
                "session_count",
            )
            _merge(
                await self._repo.facts_per_day(user_id, start, end_exclusive, project_id),
                "fact_count",
            )
            _merge(
                await self._repo.extractions_per_day(
                    user_id, start, end_exclusive, project_id
                ),
                "extraction_count",
            )
            _merge(
                await self._repo.observations_per_day(
                    user_id, start, end_exclusive, project_id
                ),
                "observation_count",
            )
            _merge(
                await self._repo.classifications_per_day(
                    user_id, start, end_exclusive, project_id
                ),
                "classification_count",
            )

        # Daily node counts (graph entities — backend-backed, not the PG stub).
        backend = await resolve_graph_backend_fail_soft(
            request, user_config, user_id, self._db, "admin_stats"
        )
        stats = GraphStatsService(self._db, backend)
        node_project_ids = await stats.resolve_project_ids(
            user_id, project_id, include_archived=True
        )
        node_per_day = await stats.entity_counts_per_day(
            user_id, node_project_ids, start, end_exclusive
        )
        for node_day, node_count in node_per_day.items():
            _ensure(node_day)["node_count"] = node_count

        if sitewide:
            _merge(
                await self._repo.edges_sitewide_per_day(
                    start, end_exclusive, project_id
                ),
                "edge_count",
            )
        else:
            _merge(
                await self._repo.edges_per_day(user_id, start, end_exclusive, project_id),
                "edge_count",
            )

        return [
            UsageStatsResponse(
                date=date_str,
                episode_count=counts["episode_count"],
                session_count=counts["session_count"],
                fact_count=counts["fact_count"],
                extraction_count=counts["extraction_count"],
                observation_count=counts["observation_count"],
                classification_count=counts["classification_count"],
                node_count=counts["node_count"],
                edge_count=counts["edge_count"],
            )
            for date_str, counts in sorted(daily_counts.items(), reverse=True)
        ]
