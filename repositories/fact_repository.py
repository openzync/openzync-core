"""Fact repository — database access for the Fact model.

Facts represent extracted knowledge triplets from conversation episodes.
This is a minimal stub for the memory wipe endpoint; full CRUD will be
added alongside the fact extraction worker (Phase 2).

Key patterns:
- Soft-delete for GDPR compliance.
- No business logic — pure query construction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class FactRepository:
    """All database access for facts.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Soft Delete by User ──────────────────────────────────────────────────

    async def soft_delete_by_user(self, user_id: UUID) -> int:
        """Soft-delete all facts for a user.

        Facts themselves don't have an ``is_deleted`` flag in the current
        schema — this method performs a logical invalidation by setting
        ``invalid_at = now()``.  For Phase 2, this will be changed to a
        proper soft-delete if ``is_deleted`` is added to the facts table.

        Args:
            user_id: The user's UUID.

        Returns:
            Number of facts invalidated.
        """
        result = await self._db.execute(
            text(
                "UPDATE facts SET invalid_at = now(), updated_at = now() "
                "WHERE user_id = :user_id AND invalid_at IS NULL"
            ),
            {"user_id": user_id},
        )
        return result.rowcount  # type: ignore[return-value]
