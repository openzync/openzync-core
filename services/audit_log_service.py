"""Business logic for audit logging.

Provides ``log_action()`` for recording events and ``query_logs()`` for
retrieving them.  The service validates inputs, enriches the payload with
request metadata, and delegates persistence to the repository.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ServiceUnavailableError
from core.sorting import SortSpec
from repositories.audit_log_repository import AuditLogRepository

if TYPE_CHECKING:
    from models.audit_log import AuditLog

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
        user_id: UUID | None,
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
            user_id: User UUID — required kwarg (None for
                unauthenticated actions).
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
            user_id=user_id,
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
    ) -> tuple[list, int]:
        """Query audit log entries with optional filters.

        Default ``created_at/desc``; whitelist ``created_at``, ``action``,
        ``status_code``, ``actor_id``.

        Args:
            user_id: Filter by user.  ``None`` means
                *platform-wide* — the caller's own isolation decides what is
                visible (an RLS-bypass session sees every row, including the
                platform-level rows that carry no user).  A concrete
                UUID restricts the result to that user.
            action: Exact-match filter on action.
            actor_id: Exact-match filter on actor.
            actor_type: Exact-match filter on actor type.
            resource_type: Exact-match filter on resource type.
            resource_id: Exact-match filter on resource ID.
            status_code: Filter by HTTP status code.
            exclude_prefix: Comma-separated action prefixes to exclude.
            created_after: ISO 8601 — include entries after this.
            created_before: ISO 8601 — include entries before this.
            limit: Max entries per page.
            offset: Pagination offset.
            sort: Validated sort spec (forwarded opaque to the repository).

        Returns:
            Tuple of (list of AuditLog ORM objects, total_count).
        """
        return await self._repo.list(
            user_id,
            action=action,
            actor_id=actor_id,
            actor_type=actor_type,
            resource_type=resource_type,
            resource_id=resource_id,
            status_code=status_code,
            exclude_prefix=exclude_prefix,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
            sort=sort,
        )

    async def list_distinct_actions(self, user_id: UUID | None) -> list[str]:
        """Return distinct action names for the user, sorted A–Z.

        Same org scoping as :meth:`query_logs` — a concrete UUID
        restricts to that user, ``None`` is platform-wide.

        Args:
            user_id: Filter by user, or ``None`` for platform-wide.

        Returns:
            Alphabetically sorted distinct action names.
        """
        return await self._repo.list_distinct_actions(user_id)

    async def resolve_actor_display_names(
        self, entries: Sequence[AuditLog]
    ) -> dict[UUID, str]:
        """Batch-resolve a human-readable actor label per entry.

        One query per actor kind for the whole page (no N+1): user
        actors resolve to name → email → sliced ID; API-key actors to
        ``name (prefix)``; ``system`` to ``"System"``; missing or
        anonymous actors to the ``"—"`` placeholder. A user/key row
        that no longer exists falls back to the sliced actor ID —
        never an error. Infra failures (e.g. DB down) still propagate
        so the 503 mapping applies; any other enrichment failure
        degrades to per-row fallbacks so the list itself never fails.

        Args:
            entries: The audit log ORM objects for the current page.

        Returns:
            Mapping of entry ID to display name — every entry in
            ``entries`` gets exactly one value.
        """
        user_ids: list[UUID] = []
        key_ids: list[UUID] = []
        for entry in entries:
            if entry.actor_type == "user" and entry.actor_id:
                try:
                    user_ids.append(UUID(entry.actor_id))
                except ValueError:
                    continue
            elif entry.actor_type == "api_key" and entry.actor_id:
                try:
                    key_ids.append(UUID(entry.actor_id))
                except ValueError:
                    continue

        try:
            users = (
                await self._repo.get_users_by_ids(user_ids) if user_ids else []
            )
            keys = (
                await self._repo.get_api_keys_by_ids(key_ids) if key_ids else []
            )
        except ServiceUnavailableError:
            raise
        except Exception:
            logger.warning("audit.actor_display_names_failed", exc_info=True)
            users, keys = [], []
        user_by_id = {u.id: u for u in users}
        key_by_id = {k.id: k for k in keys}

        resolved: dict[UUID, str] = {}
        for entry in entries:
            actor_id = entry.actor_id
            if entry.actor_type == "system":
                resolved[entry.id] = "System"
            elif (
                entry.actor_type is None
                or actor_id is None
                or actor_id == "anonymous"
            ):
                resolved[entry.id] = "—"
            elif entry.actor_type == "user":
                try:
                    user = user_by_id.get(UUID(actor_id))
                except ValueError:
                    user = None
                if user is not None:
                    resolved[entry.id] = user.name or user.email or actor_id[:8]
                else:
                    resolved[entry.id] = actor_id[:8]
            elif entry.actor_type == "api_key":
                try:
                    key = key_by_id.get(UUID(actor_id))
                except ValueError:
                    key = None
                if key is None:
                    resolved[entry.id] = actor_id[:8]
                elif key.name and key.prefix:
                    resolved[entry.id] = f"{key.name} ({key.prefix})"
                else:
                    resolved[entry.id] = key.name or key.prefix or actor_id[:8]
            else:
                resolved[entry.id] = actor_id[:8] if actor_id else "—"
        return resolved
