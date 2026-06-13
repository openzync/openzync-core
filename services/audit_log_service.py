"""Business logic for audit logging.

Provides ``log_action()`` for recording events and ``query_logs()`` for
retrieving them.  The service validates inputs, enriches the payload with
request metadata, and delegates persistence to the repository.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from repositories.audit_log_repository import AuditLogRepository

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

VALID_ACTOR_TYPES = frozenset({"user", "api_key", "system"})
"""Accepted actor_type values — enforced by DB CHECK constraint."""

# ── Service ────────────────────────────────────────────────────────────────────


class AuditLogService:
    """Service for recording and querying audit log entries."""

    def __init__(self, db: AsyncSession) -> None:
        self._repo = AuditLogRepository(db)

    async def log_action(
        self,
        *,
        organization_id: UUID | None = None,
        actor_id: str | None = None,
        actor_type: str | None = None,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details: dict | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Record an audit log entry.

        Args:
            organization_id: Organization UUID (nullable for unauthenticated actions).
            actor_id: Identifier of the acting entity.
            actor_type: ``user``, ``api_key``, or ``system``.
            action: The action performed (e.g. ``session.create``).
            resource_type: Type of resource affected.
            resource_id: Identifier of the affected resource (nullable).
            details: Arbitrary JSON payload with action-specific context.
            ip_address: Source IP address.

        Raises:
            ValueError: If ``actor_type`` is not a valid value.
        """
        if actor_type is not None and actor_type not in VALID_ACTOR_TYPES:
            logger.warning(
                "audit.invalid_actor_type",
                extra={"actor_type": actor_type, "action": action},
            )
            raise ValueError(
                f"Invalid actor_type '{actor_type}'. "
                f"Must be one of: {', '.join(sorted(VALID_ACTOR_TYPES))}"
            )

        await self._repo.create(
            organization_id=organization_id,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=ip_address,
        )

    async def query_logs(
        self,
        organization_id: UUID | None,
        *,
        action: str | None = None,
        actor_id: str | None = None,
        actor_type: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        status_code: int | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list, int]:
        """Query audit log entries with optional filters.

        Args:
            organization_id: Filter by organization (from auth context).
            action: Exact-match filter on action.
            actor_id: Exact-match filter on actor.
            actor_type: Exact-match filter on actor type.
            resource_type: Exact-match filter on resource type.
            resource_id: Exact-match filter on resource ID.
            status_code: Filter by HTTP status code.
            created_after: ISO 8601 — include entries after this.
            created_before: ISO 8601 — include entries before this.
            limit: Max entries per page.
            offset: Pagination offset.

        Returns:
            Tuple of (list of AuditLog ORM objects, total_count).
        """
        return await self._repo.list(
            organization_id=organization_id,
            action=action,
            actor_id=actor_id,
            actor_type=actor_type,
            resource_type=resource_type,
            resource_id=resource_id,
            status_code=status_code,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
        )
