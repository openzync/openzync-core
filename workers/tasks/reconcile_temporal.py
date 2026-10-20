"""Temporal-consistency scan — warn-only ARQ cron over all projects.

Runs daily at 04:00 UTC as a low-priority task.  For every (non-archived)
project it runs :class:`TemporalValidationService`'s two project-scoped
warn-only checks — cross-episode temporal overlaps and invalid/future-dated
fact ranges — and returns the warning count for the cron log.

The service never mutates data; each warning is emitted through the
service's logger and returned as a list, which this scan counts.
"""

from __future__ import annotations

from typing import Any

import structlog

from core import rls
from repositories.fact_repository import FactRepository
from repositories.project_repository import ProjectRepository
from services.temporal_service import TemporalValidationService
from workers.tasks.base import with_task_metrics

logger = structlog.get_logger()

RECONCILE_BATCH_SIZE: int = 100
"""Maximum number of projects scanned per cron tick."""


@with_task_metrics
async def reconcile_temporal(ctx: dict[str, Any]) -> str:
    """Scan every (non-archived) project for temporal-consistency issues.

    Args:
        ctx: ARQ worker context dict containing ``db_session_factory``.

    Returns:
        A summary string for the cron log, e.g.
        ``"Temporal scan: 3 warnings across 5 projects"`` or
        ``"Temporal scan: no warnings across 5 projects"``.

    Raises:
        Exception: If the DB query or validation fails (logged by the ARQ cron).
    """
    session_factory = ctx.get("db_session_factory")
    if session_factory is None:
        logger.error("reconcile_temporal.no_session_factory")
        return "Skipped: no db_session_factory in ARQ ctx"

    total_warnings = 0

    async with session_factory() as db:
        # Cross-org cron scan: bypass RLS so every org is visible.  The
        # policies read NULLIF(current_setting('app.org_id', true), ''),
        # so an unset context matches no rows — the bypass is what makes
        # the platform-wide scan possible.  The bypass is bound on the
        # session, so it survives a commit instead of silently vanishing.
        await rls.set_bypass_rls(db, True)

        projects = await ProjectRepository(db).list_all(limit=RECONCILE_BATCH_SIZE)

        for project in projects:
            service = TemporalValidationService(FactRepository(db))
            total_warnings += len(
                await service.check_project_temporal_consistency(
                    project_id=project.id,
                    organization_id=project.organization_id,
                )
            )
            total_warnings += len(
                await service.check_fact_ranges(
                    project_id=project.id,
                    organization_id=project.organization_id,
                )
            )

    logger.info(
        "reconcile_temporal.completed",
        warnings=total_warnings,
        projects=len(projects),
    )
    if total_warnings:
        return (
            f"Temporal scan: {total_warnings} warnings across {len(projects)} projects"
        )
    return f"Temporal scan: no warnings across {len(projects)} projects"
