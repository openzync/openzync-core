"""ARQ task for writing audit log entries.

Enqueued by ``AuditMiddleware`` after every request completes.
Runs on the low-priority queue — audit is non-urgent and must not
block data-processing tasks.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import orjson
import structlog

from core.rls import release_tenant_context
from services.audit_log_service import AuditLogService
from services.worker.tasks.base import with_task_metrics

logger = logging.getLogger(__name__)


@with_task_metrics
async def write_audit_log(
    ctx: dict[str, Any],
    *,
    organization_id: str | None = None,
    actor_id: str | None = None,
    actor_type: str | None = None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: str | None = None,
    ip_address: str | None = None,
    trace_id: str = "",
) -> None:
    """ARQ task — writes a single audit log entry.

    Creates its own DB session (does not rely on ``ctx`` for one)
    so that audit jobs are self-contained and survive worker restarts.

    Args:
        ctx: ARQ context (contains ``redis``, ``job_id``, etc.).
        organization_id: Organization UUID as string (nullable).
        actor_id: Identifier of the acting entity.
        actor_type: ``user``, ``api_key``, or ``system``.
        action: The action performed (e.g. ``session.create``).
        resource_type: Type of resource affected.
        resource_id: Identifier of the affected resource.
        details: JSON-encoded string of action-specific context.
        ip_address: Source IP address.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    from core import rls
    from core.config import settings
    from core.db import get_async_session, init_db_engine

    org_uuid: UUID | None = UUID(organization_id) if organization_id else None
    parsed_details: dict[str, Any] = orjson.loads(details.encode()) if details else {}

    _engine = init_db_engine(
        str(settings.DATABASE_URL),
        pool_size=2,
        max_overflow=2,
    )
    _session_factory = get_async_session(_engine)

    try:
        async with _session_factory() as db_session:
            try:
                # ``audit_logs`` is tenant-scoped — initialise the context
                # before the row is written, or the insert is rejected by
                # the policy.  An organisation-less row (a platform-level
                # audit of an unauthenticated request) belongs to no tenant,
                # and the tightened policy admits no such row to a
                # tenant-scoped session: it is written under an explicit
                # RLS bypass, on a session that does nothing else.
                await rls.init_context(
                    db_session, str(org_uuid) if org_uuid is not None else None
                )
                if org_uuid is None:
                    await rls.set_bypass_rls(db_session, True)
                service = AuditLogService(db_session)
                await service.log_action(
                    organization_id=org_uuid,
                    actor_id=actor_id,
                    actor_type=actor_type,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    details=parsed_details,
                    ip_address=ip_address,
                )
            finally:
                await release_tenant_context(db_session)
    except Exception:
        logger.exception(
            "audit_task.write_failed",
            extra={"action": action, "job_id": ctx.get("job_id")},
        )
        raise
    finally:
        await _engine.dispose()
