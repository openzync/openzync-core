"""Fact repository — all database access for extracted facts.

Facts represent extracted knowledge triplets from conversation episodes.
This repository provides CRUD operations used by both the fact extraction
worker and the memory wipe endpoint.

Key patterns:
- ORM-based operations for single-fact create (type-safe, triggers
  SQLAlchemy event listeners).
- Bulk soft-delete via ``update()`` for GDPR compliance.
- No business logic — pure query construction.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from models.fact import Fact


class FactRepository:
    """All database access for facts.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(
        self,
        user_id: UUID,
        organization_id: UUID,
        content: str,
        subject: str | None = None,
        predicate: str | None = None,
        obj: str | None = None,
        confidence: float = 1.0,
        source_episode_id: UUID | None = None,
        valid_from: datetime | None = None,
    ) -> Fact:
        """Insert a new fact and return the ORM instance.

        Args:
            user_id: FK to the owning user.
            organization_id: Denormalized org ID for RLS.
            content: Human-readable fact statement.
            subject: Subject entity of the triple.
            predicate: Relationship verb of the triple.
            obj: Object entity of the triple (named ``obj`` to avoid
                shadowing Python's built-in ``object``).
            confidence: Extraction confidence (0.0–1.0).
            source_episode_id: Optional FK back to the source episode.
            valid_from: Temporal validity start (defaults to now).

        Returns:
            The newly created :class:`Fact` instance with server-generated
            fields (id, created_at, updated_at) populated via ``refresh``.
        """
        fact = Fact(
            user_id=user_id,
            organization_id=organization_id,
            content=content,
            subject=subject,
            predicate=predicate,
            object=obj,
            confidence=confidence,
            source_episode_id=source_episode_id,
            valid_from=valid_from or datetime.now(),
        )
        self._db.add(fact)
        await self._db.flush()
        await self._db.refresh(fact)
        return fact

    # ── Soft Delete by User ──────────────────────────────────────────────────

    async def soft_delete_by_user(self, user_id: UUID) -> int:
        """Soft-delete all facts for a user by setting ``invalid_at``.

        Uses the ORM ``update()`` construct rather than raw SQL so that
        SQLAlchemy's column-level ``onupdate`` hook fires for
        ``updated_at`` on each row.

        Args:
            user_id: The user's UUID.

        Returns:
            Number of facts invalidated.
        """
        now = datetime.now()
        result = await self._db.execute(
            update(Fact)
            .where(Fact.user_id == user_id)
            .where(Fact.invalid_at.is_(None))
            .values(invalid_at=now, updated_at=now)
        )
        await self._db.flush()
        return result.rowcount  # type: ignore[return-value]
