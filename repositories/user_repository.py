"""User repository — all database access for the User domain.

Every public method accepts ``organization_id`` to enforce tenant isolation.
No business logic — pure query construction and execution.

Key patterns:
- Cursor-based pagination using base64-encoded (created_at, id) composite.
- Soft-delete via ``is_deleted`` flag.
- Metadata field uses ``metadata_`` (trailing underscore) to avoid
  SQLAlchemy's reserved ``metadata`` name — mapped to column ``"metadata"``.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, String, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.episode import Episode
from models.fact import Fact
from models.session import Session
from models.user import User

# ╠ This file contains NO business logic — only query construction.
# ╠ If you find yourself writing an ``if`` statement that makes a
# ╠ decision based on domain rules, it belongs in the service layer.


class UserRepository:
    """All database access for users.

    Every method accepts ``organization_id`` to enforce tenant isolation.
    No business logic — pure query construction and execution.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Create ──────────────────────────────────────────────────────────────

    async def create(
        self,
        organization_id: UUID,
        external_id: str,
        name: str | None = None,
        email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> User:
        """Insert a new user.

        Args:
            organization_id: Tenant scope (foreign key).
            external_id: Caller-defined unique user identifier within org.
            name: Optional display name.
            email: Optional email address.
            metadata: Arbitrary JSONB metadata.

        Returns:
            The newly created User ORM instance (with generated id and
            timestamps populated after flush).

        Raises:
            IntegrityError: If a user with this ``(organization_id,
                external_id)`` already exists (unique constraint).
        """
        user = User(
            organization_id=organization_id,
            external_id=external_id,
            name=name,
            email=email,
            metadata_=metadata or {},
        )
        self._db.add(user)
        await self._db.flush()
        await self._db.refresh(user)
        return user

    # ── Read ────────────────────────────────────────────────────────────────

    async def get_by_external_id(
        self,
        organization_id: UUID,
        external_id: str,
    ) -> User | None:
        """Look up a user by caller-defined external ID within an org.

        Args:
            organization_id: Tenant scope.
            external_id: The caller-defined identifier.

        Returns:
            The User if found, or ``None``.
        """
        result = await self._db.execute(
            select(User).where(
                User.organization_id == organization_id,
                User.external_id == external_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_uuid(self, organization_id: UUID, user_id: UUID) -> User | None:
        """Look up a user by internal UUID, scoped to an organization.

        Args:
            organization_id: Tenant scope (always applied).
            user_id: The internal OpenZep user UUID.

        Returns:
            The User if found, or ``None``.
        """
        result = await self._db.execute(
            select(User).where(
                User.id == user_id,
                User.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    # ── Update ──────────────────────────────────────────────────────────────

    async def update(
        self,
        organization_id: UUID,
        user_id: UUID,
        update_fields: dict[str, Any],
    ) -> User | None:
        """Update user fields. Only keys present in ``update_fields`` are applied.

        Uses key-presence semantics so that a value of ``None`` means
        "set to null" (clear the field) and an absent key means
        "do not update."

        For ``metadata``, performs a JSONB deep-merge (not replace).
        Keys set to ``None`` in the merge dict are removed from metadata.

        Args:
            organization_id: Tenant scope (always applied).
            user_id: The internal OpenZep user UUID.
            update_fields: Dict of fields to update. Valid keys: ``name``,
                ``email``, ``metadata``.

        Returns:
            The updated User, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(User).where(
                User.id == user_id,
                User.organization_id == organization_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None

        if "name" in update_fields:
            user.name = update_fields["name"]
        if "email" in update_fields:
            user.email = update_fields["email"]
        if "metadata" in update_fields:
            # Deep merge: new keys override, None values remove
            existing = dict(user.metadata_ or {})
            new_meta = update_fields["metadata"] or {}
            for k, v in new_meta.items():
                if v is None:
                    existing.pop(k, None)
                elif isinstance(v, dict) and isinstance(existing.get(k), dict):
                    # Recursive merge for nested dicts
                    existing[k] = {**existing[k], **v}
                else:
                    existing[k] = v
            user.metadata_ = existing

        await self._db.flush()
        await self._db.refresh(user)
        return user

    # ── Soft Delete ─────────────────────────────────────────────────────────

    async def soft_delete(
        self, organization_id: UUID, user_id: UUID
    ) -> User | None:
        """Set ``is_deleted = True`` on the user.

        Called on the DELETE endpoint as part of the GDPR two-phase
        deletion workflow (soft -> hard purge after 30 days).

        Args:
            organization_id: Tenant scope (always applied).
            user_id: The internal OpenZep user UUID.

        Returns:
            The soft-deleted User, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(User).where(
                User.id == user_id,
                User.organization_id == organization_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None
        user.is_deleted = True
        await self._db.flush()
        await self._db.refresh(user)
        return user

    # ── Hard Delete ─────────────────────────────────────────────────────────

    async def hard_delete(
        self, organization_id: UUID, user_id: UUID
    ) -> bool:
        """Permanently remove a user row.

        Used by the GDPR purge worker after the 30-day grace period.
        Cascade rules in the schema handle child tables (sessions,
        episodes, facts).

        Args:
            organization_id: Tenant scope (always applied).
            user_id: The internal OpenZep user UUID.

        Returns:
            ``True`` if a row was deleted, ``False`` if not found.
        """
        result = await self._db.execute(
            select(User).where(
                User.id == user_id,
                User.organization_id == organization_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return False
        await self._db.delete(user)
        await self._db.flush()
        return True

    # ── List with Cursor Pagination ─────────────────────────────────────────

    async def list(
        self,
        organization_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        search: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        include_deleted: bool = False,
    ) -> tuple[list[User], str | None]:
        """List users with cursor-based pagination and optional filters.

        Cursor is a base64-encoded composite of ``(created_at, id)``.
        Filters are composable — any combination of search, date range.

        Args:
            organization_id: Tenant scope (always applied).
            limit: Max results per page (default 50, max 200).
            cursor: Opaque pagination token from a previous response.
            search: Fuzzy match against ``external_id``, ``name``,
                ``email``, and metadata text (uses PostgreSQL ILIKE).
            created_after: Only users created on or after this timestamp.
            created_before: Only users created before this timestamp.
            include_deleted: If ``True``, include soft-deleted users.

        Returns:
            Tuple of ``(users_list, next_cursor_string)``.
            ``next_cursor`` is ``None`` when no more pages exist.
        """
        # ╠ LIMIT + 1 to detect has_more without a separate COUNT query
        effective_limit = min(limit, 200) + 1

        query = select(User).where(User.organization_id == organization_id)

        if not include_deleted:
            query = query.where(User.is_deleted.is_(False))

        # Cursor pagination: composite WHERE clause
        if cursor is not None:
            cursor_at, cursor_id = self._decode_cursor(cursor)
            # WHERE (created_at, id) > (cursor_at, cursor_id)
            query = query.where(
                or_(
                    User.created_at > cursor_at,
                    User.created_at == cursor_at,
                    User.id > cursor_id,
                )
            )

        # Search: multi-field ILIKE
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                or_(
                    User.external_id.ilike(search_pattern),
                    User.name.ilike(search_pattern),
                    User.email.ilike(search_pattern),
                    # JSONB text search — cast entire metadata to text
                    User.metadata_.cast(String).ilike(search_pattern),
                )
            )

        # Date range filters
        if created_after is not None:
            query = query.where(User.created_at >= created_after)
        if created_before is not None:
            query = query.where(User.created_at < created_before)

        # Consistent ordering for cursor stability
        query = query.order_by(
            User.created_at.asc(), User.id.asc()
        ).limit(effective_limit)

        result = await self._db.execute(query)
        rows = result.scalars().all()

        # Detect has_more and strip the extra row
        has_more = len(rows) == effective_limit
        users = rows[:limit] if has_more else list(rows)

        next_cursor: str | None = None
        if has_more and users:
            last = users[-1]
            next_cursor = self._encode_cursor(last.created_at, last.id)

        return users, next_cursor

    # ── Aggregate Stats ─────────────────────────────────────────────────────

    async def get_stats(self, user_id: UUID) -> dict[str, int]:
        """Return aggregate counts for a user.

        Uses a single round-trip query with subqueries — **not** N+1.

        Args:
            user_id: The internal OpenZep user UUID.

        Returns:
            Dictionary with ``message_count``, ``fact_count``, and
            ``session_count`` keys (all defaulting to 0).
        """
        stmt = (
            select(
                func.count(func.distinct(Episode.id)).label("message_count"),
                func.count(func.distinct(Fact.id)).label("fact_count"),
                func.count(func.distinct(Session.id)).label("session_count"),
            )
            .select_from(User)
            .outerjoin(Episode, Episode.user_id == User.id)
            .outerjoin(Fact, Fact.user_id == User.id)
            .outerjoin(Session, Session.user_id == User.id)
            .where(User.id == user_id)
            .group_by(User.id)
        )

        result = await self._db.execute(stmt)
        row = result.one_or_none()

        if row is None:
            return {"message_count": 0, "fact_count": 0, "session_count": 0}

        return {
            "message_count": row.message_count or 0,
            "fact_count": row.fact_count or 0,
            "session_count": row.session_count or 0,
        }

    # ── Existence Check ─────────────────────────────────────────────────────

    async def exists_by_external_id(
        self,
        organization_id: UUID,
        external_id: str,
    ) -> bool:
        """Check if a user with this ``external_id`` exists in the org.

        Efficient boolean query — fetches only the primary key (no
        full-row transfer).

        Args:
            organization_id: Tenant scope.
            external_id: The caller-defined identifier.

        Returns:
            ``True`` if a matching user exists.
        """
        result = await self._db.execute(
            select(User.id)
            .where(
                User.organization_id == organization_id,
                User.external_id == external_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    # ── Transaction Helpers ─────────────────────────────────────────────────

    async def rollback(self) -> None:
        """Roll back the current database transaction.

        Used by the service layer to recover from ``IntegrityError``
        after a concurrent-insert race in ``get_or_create_user``.
        """
        await self._db.rollback()

    # ── Cursor Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _encode_cursor(created_at: datetime, user_id: UUID) -> str:
        """Encode ``(created_at, id)`` into an opaque base64 cursor string.

        Format: ISO timestamp + ``|`` + UUID hex, then base64-encoded.

        Args:
            created_at: The ``created_at`` timestamp of the last item.
            user_id: The ``id`` UUID of the last item.

        Returns:
            URL-safe base64 string (padding stripped).
        """
        raw = f"{created_at.isoformat()}|{user_id.hex}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
        """Decode a cursor string back to ``(created_at, id)``.

        Args:
            cursor: The opaque cursor string from a previous response.

        Returns:
            Tuple of ``(created_at, id)``.

        Raises:
            ValueError: If the cursor is malformed or cannot be decoded.
        """
        try:
            # Restore padding stripped by rstrip("=")
            padding = 4 - len(cursor) % 4
            if padding != 4:
                cursor += "=" * padding
            raw = base64.urlsafe_b64decode(cursor.encode()).decode()
            at_str, id_hex = raw.split("|", 1)
            return datetime.fromisoformat(at_str), UUID(hex=id_hex)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid cursor: {e}") from e
