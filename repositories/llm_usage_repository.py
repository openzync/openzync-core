"""Repository for LLM usage reads and metering inserts.

All DB access for the ``llm_usage`` table lives here. The table is
append-only — inserts conflict-do-nothing on ``idempotency_key`` and
there is no UPDATE or DELETE at the application layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm import UsageRecord
from core.sorting import SortSpec, resolve_order_by
from models.llm_usage import LLMUsage

LLM_USAGE_SORTABLE_COLUMNS = {
    "created_at": LLMUsage.created_at,
    "duration_ms": LLMUsage.duration_ms,
    "model": LLMUsage.model,
    "prompt_tokens": LLMUsage.prompt_tokens,
    "completion_tokens": LLMUsage.completion_tokens,
    "total_tokens": LLMUsage.total_tokens,
}
"""Sortable columns for the admin LLM usage endpoint (default created_at/desc)."""


@dataclass(frozen=True)
class UsageSummary:
    """Aggregate metering totals over a filtered window."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    avg_duration_ms: float = 0.0


class LLMUsageRepository:
    """Handles all DB operations for the ``llm_usage`` table."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, record: UsageRecord) -> None:
        """Insert a metering record, ignoring idempotency-key conflicts.

        Args:
            record: Metering payload (org scope already filled by the sink).
        """
        stmt = (
            pg_insert(LLMUsage)
            .values(
                organization_id=record.org_id,
                provider=record.provider,
                model=record.model,
                task_type=record.worker,
                worker=record.worker,
                project_id=record.project_id,
                episode_id=record.episode_id,
                session_id=record.session_id,
                community_id=record.community_id,
                task_run_id=record.task_run_id,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                reasoning_tokens=record.reasoning_tokens,
                cache_read_input_tokens=record.cache_read_input_tokens,
                cache_creation_input_tokens=record.cache_creation_input_tokens,
                embed_count=record.embed_count,
                embed_dim=record.embed_dim,
                idempotency_key=record.idempotency_key,
                duration_ms=record.duration_ms,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )
        await self._db.execute(stmt)

    @classmethod
    async def create_scoped(
        cls, session_factory: Callable[[], Any], record: UsageRecord
    ) -> None:
        """Insert a metering record via an isolated RLS-scoped session.

        Opens a fresh session from ``session_factory``, binds it to the
        record's org, inserts, and commits. Used by the usage sink's
        factory path (workers) so metering never joins the caller's
        transaction.

        Args:
            session_factory: Callable returning a new async session
                (e.g. ``async_sessionmaker``).
            record: Metering payload (org scope already filled).

        Raises:
            RuntimeError: If the record has no org scope.
        """
        if record.org_id is None:
            raise RuntimeError("usage sink received a record without org_id")
        async with session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.org_id', :oid, true)"),
                {"oid": str(record.org_id)},
            )
            await cls(session).create(record)
            await session.commit()

    async def list_paginated(
        self,
        organization_id: UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        project_id: UUID | None = None,
        worker: str | None = None,
        model: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: SortSpec | None = None,
    ) -> tuple[list[LLMUsage], int]:
        """List metering rows with optional filters.

        Default ``created_at/desc``; whitelist ``created_at``,
        ``duration_ms``, ``model``, ``prompt_tokens``,
        ``completion_tokens``, ``total_tokens``.

        Args:
            organization_id: Owning org (RLS also scopes the session).
            start: Include rows created at/after this instant.
            end: Include rows created before this instant (exclusive).
            project_id: Exact-match filter on project scope.
            worker: Exact-match filter on worker label.
            model: Exact-match filter on model identifier.
            limit: Max rows per page.
            offset: Pagination offset.
            sort: Validated sort spec.

        Returns:
            Tuple of (rows, total_count).
        """
        base = select(LLMUsage).where(LLMUsage.organization_id == organization_id)
        count_base = select(func.count(LLMUsage.id)).where(
            LLMUsage.organization_id == organization_id
        )
        conditions = []
        if start is not None:
            conditions.append(LLMUsage.created_at >= start)
        if end is not None:
            conditions.append(LLMUsage.created_at < end)
        if project_id is not None:
            conditions.append(LLMUsage.project_id == project_id)
        if worker is not None:
            conditions.append(LLMUsage.worker == worker)
        if model is not None:
            conditions.append(LLMUsage.model == model)

        if conditions:
            base = base.where(*conditions)
            count_base = count_base.where(*conditions)

        total_result = await self._db.execute(count_base)
        total: int = total_result.scalar() or 0

        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("created_at", "desc")
        query = (
            base.order_by(
                *resolve_order_by(
                    LLM_USAGE_SORTABLE_COLUMNS,
                    LLMUsage.id,
                    req_sort,
                    req_dir,
                    default_sort_by="created_at",
                    default_dir="desc",
                )
            )
            .limit(limit)
            .offset(offset)
        )
        result = await self._db.execute(query)
        return list(result.scalars().all()), total

    async def get_summary(
        self,
        organization_id: UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        project_id: UUID | None = None,
        worker: str | None = None,
        model: str | None = None,
    ) -> UsageSummary:
        """Aggregate metering totals over the filtered window.

        Args:
            organization_id: Owning org (RLS also scopes the session).
            start: Include rows created at/after this instant.
            end: Include rows created before this instant (exclusive).
            project_id: Exact-match filter on project scope.
            worker: Exact-match filter on worker label.
            model: Exact-match filter on model identifier.

        Returns:
            A ``UsageSummary`` with call counts, token sums, and the
            average call duration.
        """
        query = select(
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsage.reasoning_tokens), 0),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.avg(LLMUsage.duration_ms), 0),
        ).where(LLMUsage.organization_id == organization_id)
        if start is not None:
            query = query.where(LLMUsage.created_at >= start)
        if end is not None:
            query = query.where(LLMUsage.created_at < end)
        if project_id is not None:
            query = query.where(LLMUsage.project_id == project_id)
        if worker is not None:
            query = query.where(LLMUsage.worker == worker)
        if model is not None:
            query = query.where(LLMUsage.model == model)

        result = await self._db.execute(query)
        row = result.one()
        return UsageSummary(
            calls=row[0] or 0,
            prompt_tokens=int(row[1] or 0),
            completion_tokens=int(row[2] or 0),
            reasoning_tokens=int(row[3] or 0),
            total_tokens=int(row[4] or 0),
            avg_duration_ms=float(row[5] or 0),
        )
