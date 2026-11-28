"""DB-backed admin metrics batch queries — service layer only, no SQL.

One query per method; each delegates to
:class:`AdminMetricsRepository` (DB) or :class:`GraphStatsService`
(graph backend) and shapes rows via :func:`shared.build_result`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from datetime import time as dtime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from repositories.admin_metrics_repository import AdminMetricsRepository
from services.admin_metrics.shared import ENRICHMENT_STATUS_LABELS, build_result
from services.graph_stats_service import GraphStatsService

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend


class DbMetricsQueriesService:
    """Runs the DB-backed predefined metric queries."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = AdminMetricsRepository(db)

    async def episodes_per_day(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> dict:
        """Daily episode counts over the window (project scope honored)."""
        _ = limit
        return await self._episodes_per_day_core(
            user_id, start, end, project_id, "episodes_per_day", sitewide
        )

    async def messages_per_day(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> dict:
        """Daily message counts — same query as episodes, alias response."""
        _ = limit
        return await self._episodes_per_day_core(
            user_id, start, end, project_id, "messages_per_day", sitewide
        )

    async def _episodes_per_day_core(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        project_id: UUID | None,
        query_name: str,
        sitewide: bool = False,
    ) -> dict:
        """Shared episodes/messages-per-day core — one query, two aliases."""
        if sitewide:
            day_rows = await self._repo.episodes_per_day_sitewide(
                start, end, project_id
            )
        else:
            day_rows = await self._repo.episodes_per_day(
                user_id, start, end, project_id
            )
        rows = [[str(day), count] for day, count in day_rows]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result(query_name, True, ["date", "count"], rows, params)

    async def activity_per_day(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> dict:
        """Daily audit-log activity (no project column — filter ignored)."""
        # AuditLog has no project column — project_id is explicitly ignored.
        _ = (limit, project_id)
        if sitewide:
            day_rows = await self._repo.activity_per_day_sitewide(start, end)
        else:
            day_rows = await self._repo.activity_per_day(user_id, start, end)
        rows = [[str(day), count] for day, count in day_rows]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result("activity_per_day", True, ["date", "count"], rows, params)

    async def entities_per_day(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        backend: GraphBackend | None = None,
        sitewide: bool = False,
    ) -> dict:
        """Daily entity counts from the graph backend (inventory scope)."""
        # Site-wide scope comes from the bypassed leg session (the project
        # fan-out below then sees every project); graph reads stay
        # best-effort on the holder's backend.
        _ = (limit, sitewide)
        stats = GraphStatsService(self._db, backend)
        # Inventory: archived projects keep their entities, so they stay in scope.
        project_ids = await stats.resolve_project_ids(
            user_id, project_id, include_archived=True
        )
        per_day = await stats.entity_counts_per_day(user_id, project_ids, start, end)
        # Row shape preserved: [[<midnight-UTC datetime str>, count]], newest first
        # (matches the old date_trunc PG output format).
        rows = [
            [
                str(datetime.combine(date.fromisoformat(day), dtime.min, tzinfo=UTC)),
                count,
            ]
            for day, count in sorted(per_day.items(), reverse=True)
        ]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result("entities_per_day", True, ["date", "count"], rows, params)

    async def facts_per_day(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> dict:
        """Daily fact counts over the window (project scope honored)."""
        _ = limit
        if sitewide:
            day_rows = await self._repo.facts_per_day_sitewide(start, end, project_id)
        else:
            day_rows = await self._repo.facts_per_day(user_id, start, end, project_id)
        rows = [[str(day), count] for day, count in day_rows]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result("facts_per_day", True, ["date", "count"], rows, params)

    async def enrichment_progress(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> dict:
        """Enrichment-status histogram (snapshot — window ignored)."""
        # Snapshot metric — the [start, end) window and limit are explicitly ignored.
        _ = (start, end, limit)
        if sitewide:
            status_rows = await self._repo.enrichment_progress_sitewide(project_id)
        else:
            status_rows = await self._repo.enrichment_progress(user_id, project_id)
        rows = [[status, count] for status, count in status_rows]
        labels = {
            str(k): v
            for k, v in ENRICHMENT_STATUS_LABELS.items()
            if any(row[0] == k for row in rows)
        }
        resp = build_result(
            "enrichment_progress", True, ["enrichment_status", "count"], rows, {}
        )
        resp["labels"] = labels
        return resp

    async def top_projects_by_episodes(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        include_archived: bool = False,
        sitewide: bool = False,
    ) -> dict:
        """Top projects by episode count (snapshot ranking — window ignored)."""
        # Snapshot ranking — the [start, end) window is explicitly ignored.
        _ = (start, end)
        if sitewide:
            rank_rows = await self._repo.top_projects_by_episodes_sitewide(
                limit, project_id, include_archived
            )
        else:
            rank_rows = await self._repo.top_projects_by_episodes(
                user_id, limit, project_id, include_archived
            )
        rows = [[name, count, is_archived] for name, count, is_archived in rank_rows]
        return build_result(
            "top_projects_by_episodes",
            True,
            ["project_name", "episode_count", "is_archived"],
            rows,
            {"limit": limit, "include_archived": include_archived},
        )

    async def top_users_by_messages(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> dict:
        """Top users by message count (snapshot ranking — window ignored)."""
        # Snapshot ranking — the [start, end) window is explicitly ignored.
        _ = (start, end)
        # note: COALESCE yields NULL only when both name and email are ''/NULL;
        # the ``or`` fallback keeps the display column never null/empty.
        if sitewide:
            rank_rows = await self._repo.top_users_by_messages_sitewide(
                limit, project_id
            )
        else:
            rank_rows = await self._repo.top_users_by_messages(
                user_id, limit, project_id
            )
        rows = [
            [display or str(user_id), count] for display, user_id, count in rank_rows
        ]
        return build_result(
            "top_users_by_messages",
            True,
            ["user", "message_count"],
            rows,
            {"limit": limit},
        )

    async def queue_depth_over_time(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        sitewide: bool = False,
    ) -> dict:
        """Daily queued-episode counts over the window."""
        _ = limit
        if sitewide:
            day_rows = await self._repo.queue_depth_over_time_sitewide(
                start, end, project_id
            )
        else:
            day_rows = await self._repo.queue_depth_over_time(
                user_id, start, end, project_id
            )
        rows = [[str(day), count] for day, count in day_rows]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result(
            "queue_depth_over_time", True, ["date", "count"], rows, params
        )
