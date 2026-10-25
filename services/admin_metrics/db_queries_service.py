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
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Daily episode counts over the window (project scope honored)."""
        _ = limit
        return await self._episodes_per_day_core(
            org_id, start, end, project_id, "episodes_per_day"
        )

    async def messages_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Daily message counts — same query as episodes, alias response."""
        _ = limit
        return await self._episodes_per_day_core(
            org_id, start, end, project_id, "messages_per_day"
        )

    async def _episodes_per_day_core(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        project_id: UUID | None,
        query_name: str,
    ) -> dict:
        """Shared episodes/messages-per-day core — one query, two aliases."""
        rows = [
            [str(day), count]
            for day, count in await self._repo.episodes_per_day(
                org_id, start, end, project_id
            )
        ]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result(query_name, True, ["date", "count"], rows, params)

    async def activity_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Daily audit-log activity (no project column — filter ignored)."""
        # AuditLog has no project column — project_id is explicitly ignored.
        _ = (limit, project_id)
        rows = [
            [str(day), count]
            for day, count in await self._repo.activity_per_day(org_id, start, end)
        ]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result("activity_per_day", True, ["date", "count"], rows, params)

    async def entities_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        backend: GraphBackend | None = None,
    ) -> dict:
        """Daily entity counts from the graph backend (inventory scope)."""
        _ = limit
        stats = GraphStatsService(self._db, backend)
        # Inventory: archived projects keep their entities, so they stay in scope.
        project_ids = await stats.resolve_project_ids(
            org_id, project_id, include_archived=True
        )
        per_day = await stats.entity_counts_per_day(org_id, project_ids, start, end)
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
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Daily fact counts over the window (project scope honored)."""
        _ = limit
        rows = [
            [str(day), count]
            for day, count in await self._repo.facts_per_day(
                org_id, start, end, project_id
            )
        ]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result("facts_per_day", True, ["date", "count"], rows, params)

    async def enrichment_progress(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Enrichment-status histogram (snapshot — window ignored)."""
        # Snapshot metric — the [start, end) window and limit are explicitly ignored.
        _ = (start, end, limit)
        rows = [
            [status, count]
            for status, count in await self._repo.enrichment_progress(
                org_id, project_id
            )
        ]
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
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        include_archived: bool = False,
    ) -> dict:
        """Top projects by episode count (snapshot ranking — window ignored)."""
        # Snapshot ranking — the [start, end) window is explicitly ignored.
        _ = (start, end)
        rows = [
            [name, count, is_archived]
            for name, count, is_archived in await self._repo.top_projects_by_episodes(
                org_id, limit, project_id, include_archived
            )
        ]
        return build_result(
            "top_projects_by_episodes",
            True,
            ["project_name", "episode_count", "is_archived"],
            rows,
            {"limit": limit, "include_archived": include_archived},
        )

    async def top_users_by_messages(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Top users by message count (snapshot ranking — window ignored)."""
        # Snapshot ranking — the [start, end) window is explicitly ignored.
        _ = (start, end)
        # note: COALESCE yields NULL only when both name and email are ''/NULL;
        # the ``or`` fallback keeps the display column never null/empty.
        rows = [
            [display or str(user_id), count]
            for display, user_id, count in await self._repo.top_users_by_messages(
                org_id, limit, project_id
            )
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
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Daily queued-episode counts over the window."""
        _ = limit
        rows = [
            [str(day), count]
            for day, count in await self._repo.queue_depth_over_time(
                org_id, start, end, project_id
            )
        ]
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result(
            "queue_depth_over_time", True, ["date", "count"], rows, params
        )
