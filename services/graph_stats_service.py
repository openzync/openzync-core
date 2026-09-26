"""Backend-backed graph aggregates for admin observability endpoints.

Replaces direct reads of the PostgreSQL ``graph_entities`` stub table
(never written by the FalkorDB/SurrealDB paths) with counts derived from
the org-configured graph backend's ``get_all_entities`` plus Python-side
aggregation.  Only aggregate numbers leave this service — entity rows are
never exposed via the admin API.

A ``None`` backend (graph disabled, or backend unresolvable) yields zeros,
not errors: admin summary endpoints must stay available in degraded mode,
mirroring how Prometheus outages degrade rather than fail the summary.
Every degraded path logs loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from repositories.project_repository import ProjectRepository

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.graph_backend.interface import GraphBackend

logger = structlog.get_logger(__name__)

_PROJECT_PAGE_SIZE: int = 200


def _parse_ts(raw: Any) -> datetime | None:
    """Parse a backend ``created_at`` value into an aware datetime.

    Returns ``None`` for missing or unparseable values — the caller counts
    the entity in totals but skips time-bucketing it.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class GraphStatsService:
    """Aggregate entity counts from the graph backend for admin endpoints."""

    def __init__(self, db: AsyncSession, backend: GraphBackend | None) -> None:
        self._db = db
        self._backend = backend

    async def resolve_project_ids(
        self, org_id: UUID, project_id: UUID | None
    ) -> list[UUID]:
        """Resolve the project scope for an org-wide or project-scoped query.

        A concrete ``project_id`` passes through; ``None`` fans out to all
        non-archived projects in the org (paginated — orgs may exceed one
        page).
        """
        if project_id is not None:
            return [project_id]
        repo = ProjectRepository(self._db)
        ids: list[UUID] = []
        offset = 0
        while True:
            page = await repo.list(
                organization_id=org_id,
                user_id=None,
                limit=_PROJECT_PAGE_SIZE,
                offset=offset,
            )
            if not page:
                break
            ids.extend(p.id for p in page)
            if len(page) < _PROJECT_PAGE_SIZE:
                break
            offset += _PROJECT_PAGE_SIZE
        return ids

    async def entity_totals(
        self, org_id: UUID, project_ids: list[UUID]
    ) -> tuple[int, int]:
        """Return ``(entities_total, entities_24h)`` across projects.

        Counts come from ``get_all_entities`` + Python-side ``len()``;
        the 24h window filters on parsed ``created_at`` (unparseable
        timestamps count toward the total only).
        """
        if self._backend is None:
            logger.warning(
                "graph_stats.no_backend_totals_zero",
                org_id=str(org_id),
            )
            return 0, 0
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        total = 0
        recent = 0
        for pid in project_ids:
            entities = await self._backend.get_all_entities(org_id, pid)
            total += len(entities)
            recent += sum(
                1
                for e in entities
                if (ts := _parse_ts(e.get("created_at"))) is not None and ts >= cutoff
            )
        return total, recent

    async def entity_counts_per_day(
        self,
        org_id: UUID,
        project_ids: list[UUID],
        start: datetime,
        end_exclusive: datetime | None,
    ) -> dict[str, int]:
        """Bucket entity creations per UTC day within ``[start, end)``.

        Returns ``{date_iso: count}`` with date-only keys (``YYYY-MM-DD``).
        Entities with missing/unparseable ``created_at`` are skipped loudly
        at debug level — they still count toward totals.
        """
        if self._backend is None:
            logger.warning(
                "graph_stats.no_backend_per_day_empty",
                org_id=str(org_id),
            )
            return {}
        per_day: dict[str, int] = {}
        skipped = 0
        for pid in project_ids:
            entities = await self._backend.get_all_entities(org_id, pid)
            for entity in entities:
                ts = _parse_ts(entity.get("created_at"))
                if ts is None:
                    skipped += 1
                    continue
                if ts < start:
                    continue
                if end_exclusive is not None and ts >= end_exclusive:
                    continue
                day = ts.date().isoformat()
                per_day[day] = per_day.get(day, 0) + 1
        if skipped:
            logger.debug(
                "graph_stats.per_day_skipped_unparseable",
                org_id=str(org_id),
                skipped=skipped,
            )
        return per_day
