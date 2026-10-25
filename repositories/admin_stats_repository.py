"""Admin stats read queries — repository layer only.

Every ``db.execute`` formerly embedded in ``routers/admin_stats.py``
lives here as a one-query method. No business logic, no response
shaping: methods return raw tuples/scalars and the service layer shapes
them for the dashboard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.dialog_classification import DialogClassification
from models.episode import Episode
from models.fact import Fact
from models.graph_observation import GraphObservation
from models.project import Project
from models.session import Session
from models.structured_extraction import StructuredExtraction


class AdminStatsRepository:
    """DB access for the admin stats dashboard."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _scalar_count(self, stmt: Select[Any]) -> int:
        """Execute a ``COUNT`` select and return the scalar (0 when none)."""
        result = await self._db.execute(stmt)
        return result.scalar() or 0

    def _window(
        self,
        column: Any,
        start: datetime,
        end_exclusive: datetime | None,
    ) -> list[Any]:
        """Build ``created_at`` window predicates for a query."""
        conds = [column >= start]
        if end_exclusive is not None:
            conds.append(column < end_exclusive)
        return conds

    async def episodes_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` episode rows in the window."""
        conds: list[Any] = [
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            *self._window(Episode.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(Episode.project_id == project_id)
        stmt = (
            select(
                func.date_trunc("day", Episode.created_at).label("date"),
                func.count(Episode.id).label("count"),
            )
            .select_from(Episode)
            .where(*conds)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def sessions_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` session rows in the window."""
        conds: list[Any] = [
            Session.organization_id == org_id,
            Session.is_deleted.is_(False),
            *self._window(Session.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(Session.project_id == project_id)
        stmt = (
            select(
                func.date_trunc("day", Session.created_at).label("date"),
                func.count(Session.id).label("count"),
            )
            .select_from(Session)
            .where(*conds)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def facts_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` fact rows in the window."""
        conds: list[Any] = [
            Fact.organization_id == org_id,
            *self._window(Fact.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(Fact.project_id == project_id)
        stmt = (
            select(
                func.date_trunc("day", Fact.created_at).label("date"),
                func.count(Fact.id).label("count"),
            )
            .select_from(Fact)
            .where(*conds)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def extractions_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` extraction rows in the window."""
        conds: list[Any] = [
            Project.organization_id == org_id,
            *self._window(StructuredExtraction.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(StructuredExtraction.project_id == project_id)
        stmt = (
            select(
                func.date_trunc("day", StructuredExtraction.created_at).label("date"),
                func.count(StructuredExtraction.id).label("count"),
            )
            .select_from(StructuredExtraction)
            .join(Project, StructuredExtraction.project_id == Project.id)
            .where(*conds)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def observations_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` observation rows in the window."""
        conds: list[Any] = [
            GraphObservation.organization_id == org_id,
            *self._window(GraphObservation.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(GraphObservation.project_id == project_id)
        stmt = (
            select(
                func.date_trunc("day", GraphObservation.created_at).label("date"),
                func.count(GraphObservation.id).label("count"),
            )
            .select_from(GraphObservation)
            .where(*conds)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def classifications_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` classification rows in the window."""
        conds: list[Any] = [
            DialogClassification.organization_id == org_id,
            *self._window(DialogClassification.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(DialogClassification.project_id == project_id)
        stmt = (
            select(
                func.date_trunc("day", DialogClassification.created_at).label("date"),
                func.count(DialogClassification.id).label("count"),
            )
            .select_from(DialogClassification)
            .where(*conds)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def edges_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` graph-relationship rows in the window."""
        params: dict[str, object] = {"org_id": str(org_id), "start": start}
        edge_where = "WHERE organization_id = :org_id AND created_at >= :start"
        if end_exclusive is not None:
            edge_where += " AND created_at < :end_exclusive"
            params["end_exclusive"] = end_exclusive
        if project_id is not None:
            edge_where += " AND project_id = :project_id"
            params["project_id"] = str(project_id)
        stmt = text(  # noqa: S608
            "SELECT date_trunc('day', created_at) AS date, COUNT(id) AS count "  # noqa: S608
            "FROM graph_relationships "  # noqa: S608
            f"{edge_where} "  # noqa: S608
            "GROUP BY date ORDER BY date DESC"  # noqa: S608
        )
        result = await self._db.execute(stmt, params)
        return [(row.date, row.count) for row in result]

    async def count_episodes(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> int:
        """Return windowed non-deleted episode count."""
        conds: list[Any] = [
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            *self._window(Episode.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(Episode.project_id == project_id)
        return await self._scalar_count(select(func.count(Episode.id)).where(*conds))

    async def count_sessions(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> int:
        """Return windowed non-deleted session count."""
        conds: list[Any] = [
            Session.organization_id == org_id,
            Session.is_deleted.is_(False),
            *self._window(Session.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(Session.project_id == project_id)
        return await self._scalar_count(select(func.count(Session.id)).where(*conds))

    async def count_facts(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> int:
        """Return windowed fact count."""
        conds: list[Any] = [
            Fact.organization_id == org_id,
            *self._window(Fact.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(Fact.project_id == project_id)
        return await self._scalar_count(select(func.count(Fact.id)).where(*conds))

    async def count_extractions(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> int:
        """Return windowed extraction count (scoped via project join)."""
        conds: list[Any] = [
            Project.organization_id == org_id,
            *self._window(StructuredExtraction.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(StructuredExtraction.project_id == project_id)
        return await self._scalar_count(
            select(func.count(StructuredExtraction.id))
            .select_from(StructuredExtraction)
            .join(Project, StructuredExtraction.project_id == Project.id)
            .where(*conds)
        )

    async def count_observations(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> int:
        """Return windowed observation count."""
        conds: list[Any] = [
            GraphObservation.organization_id == org_id,
            *self._window(GraphObservation.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(GraphObservation.project_id == project_id)
        return await self._scalar_count(
            select(func.count(GraphObservation.id)).where(*conds)
        )

    async def count_classifications(
        self,
        org_id: UUID,
        start: datetime,
        end_exclusive: datetime | None,
        project_id: UUID | None,
    ) -> int:
        """Return windowed classification count."""
        conds: list[Any] = [
            DialogClassification.organization_id == org_id,
            *self._window(DialogClassification.created_at, start, end_exclusive),
        ]
        if project_id is not None:
            conds.append(DialogClassification.project_id == project_id)
        return await self._scalar_count(
            select(func.count(DialogClassification.id)).where(*conds)
        )
