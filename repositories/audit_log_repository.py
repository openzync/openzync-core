"""Repository for audit log CRUD operations.

All DB access for the ``audit_logs`` table lives here.
The table is append-only — no UPDATE or DELETE at the application layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.audit_log import AuditLog


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp into a timezone-aware datetime (UTC).

    ``fromisoformat`` only understands ``+00:00`` offsets, so a trailing
    ``Z`` is normalized first. Naive timestamps are assumed to be UTC so
    they compare correctly against the ``timestamptz`` column.

    Raises:
        ValueError: If ``ts`` is not a valid ISO 8601 timestamp.
    """
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"Invalid ISO 8601 timestamp: {ts!r} "
            "(expected e.g. '2026-07-31T10:00:00Z')"
        ) from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


class AuditLogRepository:
    """Handles all DB operations for the ``audit_logs`` table."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        organization_id: UUID | None,
        actor_id: str | None,
        actor_type: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        details: dict | None,
        ip_address: str | None,
    ) -> AuditLog:
        """Insert a new audit log entry.

        Args:
            organization_id: Organization UUID (nullable for unauthenticated actions).
            actor_id: Identifier of the acting entity.
            actor_type: ``user``, ``api_key``, or ``system``.
            action: The action performed (e.g. ``session.create``).
            resource_type: Type of resource affected.
            resource_id: Identifier of the affected resource (nullable).
            details: Arbitrary JSON payload with action-specific context.
            ip_address: Source IP address.

        Returns:
            The newly created ``AuditLog`` instance.
        """
        entry = AuditLog(
            organization_id=organization_id,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details if details is not None else {},
            ip_address=ip_address,
        )
        self._db.add(entry)
        await self._db.flush()
        await self._db.commit()
        await self._db.refresh(entry)
        return entry

    async def list(
        self,
        organization_id: UUID | None,
        *,
        action: str | None = None,
        actor_id: str | None = None,
        actor_type: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        status_code: int | None = None,
        exclude_prefix: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditLog], int]:
        """Query audit log entries with optional filters.

        Args:
            organization_id: Filter by org (passed from service).
            action: Exact-match filter on action.
            actor_id: Exact-match filter on actor_id.
            actor_type: Exact-match filter on actor_type.
            resource_type: Exact-match filter on resource_type.
            resource_id: Exact-match filter on resource_id.
            status_code: Exact-match filter on details->>status_code.
            exclude_prefix: Comma-separated action prefixes to exclude.
            created_after: ISO 8601 timestamp (e.g. ``2026-07-31T10:00:00Z``)
                — include entries after this.
            created_before: ISO 8601 timestamp (e.g. ``2026-07-31T10:00:00Z``)
                — include entries before this.
            limit: Max entries per page.
            offset: Pagination offset.

        Returns:
            Tuple of (entries, total_count).
        """
        # Base query — RLS handles org isolation via organization_id
        base = select(AuditLog)
        count_base = select(func.count(AuditLog.id))

        conditions = []
        if action is not None:
            conditions.append(AuditLog.action == action)
        if actor_id is not None:
            conditions.append(AuditLog.actor_id == actor_id)
        if actor_type is not None:
            conditions.append(AuditLog.actor_type == actor_type)
        if resource_type is not None:
            conditions.append(AuditLog.resource_type == resource_type)
        if resource_id is not None:
            conditions.append(AuditLog.resource_id == resource_id)
        if status_code is not None:
            conditions.append(AuditLog.details["status_code"].as_integer() == status_code)
        if exclude_prefix:
            prefixes = [p.strip() for p in exclude_prefix.split(",") if p.strip()]
            if prefixes:
                conditions.append(
                    not_(
                        or_(
                            *[AuditLog.action.startswith(p) for p in prefixes]
                        )
                    )
                )
        if created_after is not None:
            conditions.append(AuditLog.created_at >= _parse_iso(created_after))
        if created_before is not None:
            conditions.append(AuditLog.created_at <= _parse_iso(created_before))

        if conditions:
            base = base.where(*conditions)
            count_base = count_base.where(*conditions)

        # Total count
        total_result = await self._db.execute(count_base)
        total: int = total_result.scalar() or 0

        # Paginated query — newest first
        query = (
            base
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._db.execute(query)
        entries = list(result.scalars().all())

        return entries, total
