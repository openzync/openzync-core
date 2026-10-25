"""Admin metrics read queries — repository layer only.

Every ``db.execute`` formerly embedded in ``routers/admin_metrics.py``
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

from models.audit_log import AuditLog
from models.episode import Episode
from models.fact import Fact
from models.project import Project
from models.user import User
from repositories.project_repository import archived_project_ids
from workers.tasks.base import ENRICHMENT_ALL


class AdminMetricsRepository:
    """DB access for the admin metrics dashboard."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _scalar_count(self, stmt: Select[Any]) -> int:
        """Execute a ``COUNT`` select and return the scalar (0 when none)."""
        result = await self._db.execute(stmt)
        return result.scalar() or 0

    async def episodes_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` episode rows in ``[start, end)``."""
        conditions = [
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            Episode.created_at >= start,
            Episode.created_at < end,
        ]
        if project_id:
            conditions.append(Episode.project_id == project_id)
        stmt = (
            select(
                func.date_trunc(
                    "day", Episode.created_at.op("AT TIME ZONE")("UTC")
                ).label("date"),
                func.count(Episode.id).label("count"),
            )
            .select_from(Episode)
            .where(*conditions)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def activity_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` audit-log rows in ``[start, end)``."""
        stmt = (
            select(
                func.date_trunc(
                    "day", AuditLog.created_at.op("AT TIME ZONE")("UTC")
                ).label("date"),
                func.count(AuditLog.id).label("count"),
            )
            .select_from(AuditLog)
            .where(
                AuditLog.organization_id == org_id,
                AuditLog.created_at >= start,
                AuditLog.created_at < end,
            )
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def facts_per_day(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` fact rows in ``[start, end)``."""
        conditions = [
            Fact.organization_id == org_id,
            Fact.created_at >= start,
            Fact.created_at < end,
        ]
        if project_id:
            conditions.append(Fact.project_id == project_id)
        stmt = (
            select(
                func.date_trunc("day", Fact.created_at.op("AT TIME ZONE")("UTC")).label(
                    "date"
                ),
                func.count(Fact.id).label("count"),
            )
            .select_from(Fact)
            .where(*conditions)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def enrichment_progress(
        self,
        org_id: UUID,
        project_id: UUID | None,
    ) -> list[tuple[int, int]]:
        """Return ``(enrichment_status, count)`` rows (snapshot, no window)."""
        conditions = [
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            Episode.project_id.not_in(archived_project_ids()),
        ]
        if project_id:
            conditions.append(Episode.project_id == project_id)
        stmt = (
            select(
                Episode.enrichment_status,
                func.count(Episode.id).label("count"),
            )
            .select_from(Episode)
            .where(*conditions)
            .group_by(Episode.enrichment_status)
            .order_by(text("count DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.enrichment_status, row.count) for row in result]

    async def top_projects_by_episodes(
        self,
        org_id: UUID,
        limit: int,
        project_id: UUID | None,
        include_archived: bool,
    ) -> list[tuple[str, int, bool]]:
        """Return ``(project_name, episode_count, is_archived)`` ranking."""
        conditions = [
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
        ]
        if project_id:
            conditions.append(Episode.project_id == project_id)
        if not include_archived:
            conditions.append(Project.is_archived.is_(False))
        stmt = (
            select(
                Project.name.label("project_name"),
                func.count(Episode.id).label("episode_count"),
                Project.is_archived.label("is_archived"),
            )
            .select_from(Episode)
            .join(Project, Project.id == Episode.project_id)
            .where(*conditions)
            .group_by(Project.id, Project.name, Project.is_archived)
            .order_by(text("episode_count DESC"))
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return [
            (row.project_name, row.episode_count, row.is_archived) for row in result
        ]

    async def top_users_by_messages(
        self,
        org_id: UUID,
        limit: int,
        project_id: UUID | None,
    ) -> list[tuple[str | None, UUID, int]]:
        """Return ``(display_name_or_none, user_id, message_count)`` ranking."""
        conditions = [
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
        ]
        if project_id:
            conditions.append(Episode.project_id == project_id)
        stmt = (
            select(
                func.coalesce(
                    func.nullif(User.name, ""),
                    func.nullif(User.email, ""),
                ).label("user"),
                User.id.label("user_id"),
                func.count(Episode.id).label("message_count"),
            )
            .select_from(Episode)
            .join(User, User.id == Episode.user_id)
            .where(User.organization_id == org_id, *conditions)
            .group_by(User.id, User.name, User.email)
            .order_by(text("message_count DESC"))
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return [(row.user, row.user_id, row.message_count) for row in result]

    async def queue_depth_over_time(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        project_id: UUID | None,
    ) -> list[tuple[datetime, int]]:
        """Return ``(day, count)`` pending-enrichment rows in ``[start, end)``."""
        conditions = [
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            (Episode.enrichment_status.op("&")(ENRICHMENT_ALL)) != ENRICHMENT_ALL,
            Episode.project_id.not_in(archived_project_ids()),
            Episode.created_at >= start,
            Episode.created_at < end,
        ]
        if project_id:
            conditions.append(Episode.project_id == project_id)
        stmt = (
            select(
                func.date_trunc(
                    "day", Episode.created_at.op("AT TIME ZONE")("UTC")
                ).label("date"),
                func.count(Episode.id).label("count"),
            )
            .select_from(Episode)
            .where(*conditions)
            .group_by(text("date"))
            .order_by(text("date DESC"))
        )
        result = await self._db.execute(stmt)
        return [(row.date, row.count) for row in result]

    async def count_episodes_total(self, org_id: UUID) -> int:
        """Return total non-deleted episode count."""
        return await self._scalar_count(
            select(func.count(Episode.id)).where(
                Episode.organization_id == org_id,
                Episode.is_deleted.is_(False),
            )
        )

    async def count_episodes_24h(self, org_id: UUID) -> int:
        """Return non-deleted episode count from the last 24 hours."""
        return await self._scalar_count(
            select(func.count(Episode.id)).where(
                Episode.organization_id == org_id,
                Episode.is_deleted.is_(False),
                Episode.created_at >= func.now() - text("interval '24 hours'"),
            )
        )

    async def count_episodes_archived(self, org_id: UUID) -> int:
        """Return non-deleted episode count in archived projects."""
        return await self._scalar_count(
            select(func.count(Episode.id)).where(
                Episode.organization_id == org_id,
                Episode.is_deleted.is_(False),
                Episode.project_id.in_(archived_project_ids()),
            )
        )

    async def count_episodes_enrichable(self, org_id: UUID) -> int:
        """Return non-deleted episode count in non-archived projects."""
        return await self._scalar_count(
            select(func.count(Episode.id)).where(
                Episode.organization_id == org_id,
                Episode.is_deleted.is_(False),
                Episode.project_id.not_in(archived_project_ids()),
            )
        )

    async def count_episodes_in_progress(self, org_id: UUID) -> int:
        """Return enrichable episode count with incomplete enrichment."""
        return await self._scalar_count(
            select(func.count(Episode.id)).where(
                Episode.organization_id == org_id,
                Episode.is_deleted.is_(False),
                (Episode.enrichment_status.op("&")(ENRICHMENT_ALL)) != ENRICHMENT_ALL,
                Episode.project_id.not_in(archived_project_ids()),
            )
        )

    async def count_episodes_pending(self, org_id: UUID) -> int:
        """Return enrichable episode count with no enrichment started."""
        return await self._scalar_count(
            select(func.count(Episode.id)).where(
                Episode.organization_id == org_id,
                Episode.is_deleted.is_(False),
                Episode.enrichment_status == 0,
                Episode.project_id.not_in(archived_project_ids()),
            )
        )

    async def count_episodes_fully_enriched(self, org_id: UUID) -> int:
        """Return enrichable episode count with all enrichment bits set."""
        return await self._scalar_count(
            select(func.count(Episode.id)).where(
                Episode.organization_id == org_id,
                Episode.is_deleted.is_(False),
                (Episode.enrichment_status.op("&")(ENRICHMENT_ALL)) == ENRICHMENT_ALL,
                Episode.project_id.not_in(archived_project_ids()),
            )
        )

    async def count_episodes_with_embeddings(self, org_id: UUID) -> int:
        """Return enrichable episode count with a stored embedding."""
        return await self._scalar_count(
            select(func.count(Episode.id)).where(
                Episode.organization_id == org_id,
                Episode.is_deleted.is_(False),
                Episode.embedding.isnot(None),
                Episode.project_id.not_in(archived_project_ids()),
            )
        )

    async def count_users_total(self, org_id: UUID) -> int:
        """Return total non-deleted user count."""
        return await self._scalar_count(
            select(func.count(User.id)).where(
                User.organization_id == org_id,
                User.is_deleted.is_(False),
            )
        )
