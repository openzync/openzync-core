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

from typing import Any

from sqlalchemy import text, update
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
            embedding=[],
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

    # ── Vector Search ─────────────────────────────────────────────────────────

    async def search_by_vector(
        self, embedding: list[float], user_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Search facts by vector similarity (pgvector cosine distance).

        Uses the ``<=>`` operator on the ``embedding`` column. The score is
        inverted (``1 - distance``) so higher = more similar. Only returns
        facts that have a non-null embedding.

        Args:
            embedding: The query embedding vector.
            user_id: Scope results to this user.
            limit: Maximum results (capped at 200).

        Returns:
            A list of dicts with keys ``id``, ``content``, ``subject``,
            ``predicate``, ``object``, ``confidence``, and ``score``.
        """
        effective_limit = min(limit, 200)
        result = await self._db.execute(
            text(
                """
                SELECT id, content, subject, predicate, "object", confidence,
                       1 - (embedding <=> :embedding) AS score
                FROM facts
                WHERE user_id = :user_id
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> :embedding
                LIMIT :limit
                """
            ),
            {"embedding": embedding, "user_id": user_id, "limit": effective_limit},
        )
        return [
            {
                "id": str(r[0]),
                "content": r[1],
                "subject": r[2],
                "predicate": r[3],
                "object": r[4],
                "confidence": float(r[5]) if r[5] is not None else 0.0,
                "score": float(r[6]),
            }
            for r in result.fetchall()
        ]

    # ── BM25 Full-Text Search ─────────────────────────────────────────────────

    async def search_by_bm25(
        self, query: str, user_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Search facts by BM25 full-text (PostgreSQL ``ts_rank``).

        Tokenises the query via ``plainto_tsquery`` and ranks results using
        ``ts_rank`` over an English text search configuration on the
        ``content`` column.

        Args:
            query: Raw search text (no special syntax needed).
            user_id: Scope results to this user.
            limit: Maximum results (capped at 200).

        Returns:
            A list of dicts with keys ``id``, ``content``, ``subject``,
            ``predicate``, ``object``, ``confidence``, and ``score``.
        """
        effective_limit = min(limit, 200)
        result = await self._db.execute(
            text(
                """
                SELECT id, content, subject, predicate, "object", confidence,
                       ts_rank(
                           to_tsvector('english', content),
                           plainto_tsquery('english', :query)
                       ) AS score
                FROM facts
                WHERE user_id = :user_id
                  AND to_tsvector('english', content)
                      @@ plainto_tsquery('english', :query)
                ORDER BY score DESC
                LIMIT :limit
                """
            ),
            {"query": query, "user_id": user_id, "limit": effective_limit},
        )
        return [
            {
                "id": str(r[0]),
                "content": r[1],
                "subject": r[2],
                "predicate": r[3],
                "object": r[4],
                "confidence": float(r[5]) if r[5] is not None else 0.0,
                "score": float(r[6]),
            }
            for r in result.fetchall()
        ]
