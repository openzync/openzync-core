"""Repository for audit log CRUD operations.

All DB access for the ``audit_logs`` table lives here.
The table is append-only — no UPDATE or DELETE at the application layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import rls
from core.sorting import SortSpec, resolve_order_by
from models.api_key import ApiKey
from models.audit_log import AuditLog
from models.user import User

AUDIT_LOG_SORTABLE_COLUMNS = {
    "created_at": AuditLog.created_at,
    "action": AuditLog.action,
    "status_code": AuditLog.details["status_code"].as_integer(),
    "actor_id": AuditLog.actor_id,
}
"""Sortable columns for admin audit logs (default created_at/desc)."""


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
        user_id: UUID | None,
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
            user_id: User UUID (nullable for unauthenticated actions).
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
            user_id=user_id,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details if details is not None else {},
            ip_address=ip_address,
        )
        # The audit writer runs on its own session (the ARQ job) with no
        # tenant context — bind the session to the entry's user so
        # the user-scoped row passes the audit_logs policy.  An entry without
        # a user-less entry is a platform-level row: no tenant-scoped policy
        # admits it, so the writer must have enabled the RLS bypass (the ARQ
        # audit task does).  Without that bypass the insert is refused by
        # the database — loudly, never silently dropped.
        if user_id is not None:
            await rls.set_tenant_context(self._db, user_id)
        self._db.add(entry)
        await self._db.flush()
        await self._db.commit()
        await self._db.refresh(entry)
        return entry

    async def list(
        self,
        user_id: UUID | None,
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
        sort: SortSpec | None = None,
    ) -> tuple[list[AuditLog], int]:
        """Query audit log entries with optional filters.

        Default ``created_at/desc``; whitelist ``created_at``, ``action``,
        ``status_code`` (``details->>status_code``), ``actor_id``.

        Args:
            user_id: Filter by user.  ``None`` means *platform-wide* —
                the caller's own isolation (an RLS-bypass session) decides
                what is visible.  A concrete UUID restricts the result to
                that user, including the exclusion of the
                platform-level rows that carry no user.
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
            sort: Validated sort spec.

        Returns:
            Tuple of (entries, total_count).
        """
        # The org filter is applied here, not left to the policy: RLS is an
        # *additional* filter, and the audit table also holds platform-level
        # rows (no user) that a tenant must never see.  A tenant
        # admin reading the audit trail must not learn another tenant's
        # authentication attempts, their client IPs or their request ids.
        base = select(AuditLog)
        count_base = select(func.count(AuditLog.id))

        conditions = []
        if user_id is not None:
            conditions.append(AuditLog.user_id == user_id)
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

        # Paginated query — newest first by default
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("created_at", "desc")
        query = (
            base.order_by(
                *resolve_order_by(
                    AUDIT_LOG_SORTABLE_COLUMNS,
                    AuditLog.id,
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
        entries = list(result.scalars().all())

        return entries, total

    async def list_distinct_actions(
        self, user_id: UUID | None
    ) -> list[str]:
        """Return distinct action names, alphabetically sorted.

        Applies the same user condition as :meth:`list` — a
        concrete UUID restricts the result to that user (and
        excludes platform-level rows with no user); ``None``
        means platform-wide (the caller's own isolation decides what
        is visible).

        Args:
            user_id: Filter by user, or ``None`` for platform-wide.

        Returns:
            Sorted list of distinct action names.
        """
        query = select(AuditLog.action).distinct()
        if user_id is not None:
            query = query.where(AuditLog.user_id == user_id)
        query = query.order_by(AuditLog.action.asc())
        result = await self._db.execute(query)
        return [row[0] for row in result.all()]

    async def get_users_by_ids(self, ids: list[UUID]) -> list[User]:
        """Fetch users for actor display-name resolution in one batch.

        Args:
            ids: User UUIDs parsed from the page's ``actor_id`` values.

        Returns:
            Matching User ORM objects (missing IDs are simply absent).
        """
        if not ids:
            return []
        result = await self._db.execute(select(User).where(User.id.in_(ids)))
        return list(result.scalars().all())

    async def get_api_keys_by_ids(self, ids: list[UUID]) -> list[ApiKey]:
        """Fetch API keys for actor display-name resolution in one batch.

        Args:
            ids: API key UUIDs parsed from the page's ``actor_id`` values.

        Returns:
            Matching ApiKey ORM objects (missing IDs are simply absent).
        """
        if not ids:
            return []
        result = await self._db.execute(select(ApiKey).where(ApiKey.id.in_(ids)))
        return list(result.scalars().all())
