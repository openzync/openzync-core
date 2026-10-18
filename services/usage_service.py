"""LLM usage metering — sink construction and read orchestration.

Write path: :func:`make_sink` builds a :class:`UsageSink
<core.llm.UsageSink>` carrying org/worker/entity scope for
:func:`core.llm.resolve_backend` callers. The sink persists records via
:class:`LLMUsageRepository <repositories.llm_usage_repository
.LLMUsageRepository>` in an isolated session (session factory) or a
savepoint (active session), logging failures then re-raising into ARQ retry.

Read path: :func:`list_usage` / :func:`summarize_usage` forward filters
to the repository for the admin read API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm import UsageRecord, UsageSink
from core.sorting import SortSpec
from repositories.llm_usage_repository import LLMUsageRepository, UsageSummary

if TYPE_CHECKING:
    from models.llm_usage import LLMUsage

logger = logging.getLogger(__name__)

SessionTarget = AsyncSession | Callable[[], AsyncSession]
"""Isolated-session factory (e.g. ``async_sessionmaker``) or an active session.

Factories get a fresh RLS-scoped session + commit; active sessions get a
savepoint owned by the caller's transaction.
"""


async def _persist(target: SessionTarget, record: UsageRecord) -> None:
    """Insert *record* via an isolated session or a savepoint.

    Args:
        target: Session factory or active session (see :data:`SessionTarget`).
        record: Complete metering payload (org scope already filled).

    Raises:
        RuntimeError: If the record has no org scope — a wiring bug that
            must stay loud inside the sink's ``try`` so it logs, never
            propagates.
    """
    if record.org_id is None:
        raise RuntimeError("usage sink received a record without org_id")
    if isinstance(target, Callable):
        async with target() as session:
            await session.execute(
                text("SELECT set_config('app.org_id', :oid, true)"),
                {"oid": str(record.org_id)},
            )
            await LLMUsageRepository(session).create(record)
            await session.commit()
    else:
        async with target.begin_nested():
            await LLMUsageRepository(target).create(record)


def make_sink(
    target: SessionTarget,
    *,
    org_id: UUID,
    worker: str,
    project_id: UUID | None = None,
    episode_id: UUID | None = None,
    session_id: UUID | None = None,
    community_id: UUID | None = None,
    task_run_id: UUID | None = None,
) -> UsageSink:
    """Build a metering sink carrying org/worker/entity scope.

    The returned sink fills scope fields the metering wrapper leaves
    empty, then persists via :func:`_persist`. Failures are logged and
    re-raised into the ARQ retry loop — never swallowed, so a metering
    outage is visible instead of silently dropping usage rows.

    Args:
        target: Session factory (workers) or active session (services).
        org_id: Owning organization.
        worker: Task vocabulary label (e.g. ``"enrich_episode"``).
        project_id: Project scope, when known.
        episode_id: Source episode, when the call enriches one.
        session_id: Source session, when known.
        community_id: Community being summarised, when applicable.
        task_run_id: Owning task run, when applicable.

    Returns:
        An async callable accepting a :class:`UsageRecord`.

    Raises:
        Exception: Whatever :func:`_persist` raised (after logging).
    """

    async def _sink(record: UsageRecord) -> None:
        """Fill scope and persist. Logs failures, then re-raises for retry."""
        full = replace(
            record,
            org_id=record.org_id or org_id,
            worker=record.worker or worker,
            project_id=record.project_id or project_id,
            episode_id=record.episode_id or episode_id,
            session_id=record.session_id or session_id,
            community_id=record.community_id or community_id,
            task_run_id=record.task_run_id or task_run_id,
        )
        try:
            await _persist(target, full)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "usage.sink_failed",
                extra={"worker": worker, "org_id": str(org_id)},
            )
            raise

    return _sink


async def list_usage(
    db: AsyncSession,
    *,
    org_id: UUID,
    start: datetime | None = None,
    end: datetime | None = None,
    project_id: UUID | None = None,
    worker: str | None = None,
    model: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: SortSpec | None = None,
) -> tuple[list[LLMUsage], int]:
    """List metering rows for an org with optional filters.

    Args:
        db: Active async session (RLS-scoped to the org).
        org_id: Owning organization.
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
    return await LLMUsageRepository(db).list_paginated(
        org_id,
        start=start,
        end=end,
        project_id=project_id,
        worker=worker,
        model=model,
        limit=limit,
        offset=offset,
        sort=sort,
    )


async def summarize_usage(
    db: AsyncSession,
    *,
    org_id: UUID,
    start: datetime | None = None,
    end: datetime | None = None,
    project_id: UUID | None = None,
    worker: str | None = None,
    model: str | None = None,
) -> UsageSummary:
    """Aggregate metering totals for an org over the filtered window.

    Args:
        db: Active async session (RLS-scoped to the org).
        org_id: Owning organization.
        start: Include rows created at/after this instant.
        end: Include rows created before this instant (exclusive).
        project_id: Exact-match filter on project scope.
        worker: Exact-match filter on worker label.
        model: Exact-match filter on model identifier.

    Returns:
        A ``UsageSummary`` with call counts, token sums, and the
        average call duration.
    """
    return await LLMUsageRepository(db).get_summary(
        org_id,
        start=start,
        end=end,
        project_id=project_id,
        worker=worker,
        model=model,
    )
