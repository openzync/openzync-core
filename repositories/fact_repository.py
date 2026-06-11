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

import logging
from datetime import datetime
from uuid import UUID

from typing import Any

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.fact import Fact

logger = logging.getLogger(__name__)


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
        subject_entity_id: UUID | None = None,
        object_entity_id: UUID | None = None,
        subject_type: str = "literal",
        object_type: str = "literal",
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
            subject_entity_id: Optional FK to ``graph_entities`` for the
                resolved subject entity.
            object_entity_id: Optional FK to ``graph_entities`` for the
                resolved object entity.
            subject_type: Entity type for the subject (``"literal"`` or
                ``"entity"`` when resolved).
            object_type: Entity type for the object (``"literal"`` or
                ``"entity"`` when resolved).

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
            subject_entity_id=subject_entity_id,
            object_entity_id=object_entity_id,
            subject_type=subject_type,
            object_type=object_type,
            embedding=[],
        )
        self._db.add(fact)
        await self._db.flush()
        await self._db.refresh(fact)
        return fact

    # ── Batch Create ────────────────────────────────────────────────────────────

    async def batch_create(
        self,
        organization_id: UUID,
        user_id: UUID,
        facts: list[dict],
    ) -> list[Fact]:
        """Bulk-insert facts using a single INSERT statement.

        More efficient than per-row ``create()`` for batch ingestion.
        Uses SQLAlchemy's ``insert()`` with ``returning()`` to fetch
        the generated IDs.

        Args:
            organization_id: Denormalized org ID for RLS.
            user_id: FK to the owning user.
            facts: List of dicts, each with optional keys: ``subject``,
                ``predicate``, ``object``, ``content``, ``confidence``,
                ``source_episode_id``, ``valid_from``.

        Returns:
            A list of created ``Fact`` ORM instances with server-generated
            fields populated.
        """
        if not facts:
            return []

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        rows = []
        for f in facts:
            subject = f.get("subject")
            predicate = f.get("predicate")
            obj = f.get("object")
            content = f.get(
                "content",
                f"{subject} {predicate} {obj}" if subject and predicate and obj else "",
            )
            rows.append(
                {
                    "user_id": user_id,
                    "organization_id": organization_id,
                    "content": content,
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "subject_type": f.get("subject_type", "literal"),
                    "object_type": f.get("object_type", "literal"),
                    "confidence": f.get("confidence", 1.0),
                    "source_episode_id": f.get("source_episode_id"),
                    "valid_from": f.get("valid_from", now),
                    "valid_to": f.get("valid_to"),
                    "subject_entity_id": f.get("subject_entity_id"),
                    "object_entity_id": f.get("object_entity_id"),
                    "embedding": [],
                }
            )

        # Bulk insert with RETURNING
        from sqlalchemy import insert

        stmt = insert(Fact).returning(Fact)
        result = await self._db.execute(stmt, rows)
        await self._db.flush()

        created = list(result.scalars().all())

        # Refresh each to populate server defaults (created_at, etc.)
        for fact in created:
            await self._db.refresh(fact)

        logger.info(
            "fact_repository.batch_created",
            extra={
                "count": len(created),
                "user_id": str(user_id),
                "organization_id": str(organization_id),
            },
        )

        return created

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

    # ── Entity Lookup ───────────────────────────────────────────────────────────

    async def get_entities_for_session(
        self,
        session_id: UUID,
        organization_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return all distinct graph entities linked to episodes in a session.

        Traverses the ``session → episodes → graph_episode_entities →
        graph_entities`` chain to collect every entity that has been
        extracted from any turn of this session.

        Args:
            session_id: The session to fetch entities for.
            organization_id: Tenant scope.

        Returns:
            A list of dicts with keys ``id``, ``name``, ``entity_type``,
            ``summary``.
        """
        result = await self._db.execute(
            text("""
                SELECT DISTINCT ge.id, ge.name, ge.entity_type, ge.summary
                FROM graph_entities ge
                JOIN graph_episode_entities gee ON ge.id = gee.entity_id
                JOIN episodes e ON e.id = gee.episode_id
                WHERE e.session_id = :session_id
                  AND e.organization_id = :org_id
                  AND ge.organization_id = :org_id
                  AND e.is_deleted = false
                  AND ge.is_merged = false
                ORDER BY ge.name
            """),
            {"session_id": session_id, "org_id": organization_id},
        )
        rows = result.mappings().all()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "entity_type": row["entity_type"],
                "summary": row["summary"],
            }
            for row in rows
        ]

    # ── List by Session ────────────────────────────────────────────────────────

    async def list_by_session(
        self,
        organization_id: UUID,
        session_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List non-invalidated facts for episodes in a session.

        Paginated by ``created_at DESC, id ASC`` using an opaque base64
        cursor.  Facts without a ``source_episode_id`` are excluded since
        they cannot be scoped to a session.

        Args:
            organization_id: Tenant scope.
            session_id: The session to fetch facts for.
            limit: Max results (1–200).
            cursor: Opaque base64 cursor from a previous page.

        Returns:
            Tuple of (list of fact dicts, next_cursor or None).
        """
        import base64
        from sqlalchemy import select, text as sql_text

        effective_limit = min(limit, 200) + 1  # +1 to detect has_more

        # Decode cursor
        cursor_created: datetime | None = None
        cursor_id: UUID | None = None
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor).decode()
                parts = decoded.split("|")
                if len(parts) == 2:
                    cursor_created = datetime.fromisoformat(parts[0])
                    cursor_id = UUID(parts[1])
            except (ValueError, TypeError):
                pass

        base_query = """
            SELECT f.id, f.content, f.subject, f.predicate,
                   f."object", f.confidence, f.source_episode_id,
                   f.created_at, f.subject_type, f.object_type,
                   f.subject_entity_id, f.object_entity_id
            FROM facts f
            WHERE f.organization_id = :org_id
              AND f.source_episode_id IN (
                  SELECT e.id FROM episodes e
                  WHERE e.session_id = :session_id AND e.is_deleted = false
              )
              AND f.invalid_at IS NULL
        """

        if cursor_id is not None:
            stmt = sql_text(
                base_query
                + """
                AND (f.created_at, f.id) < (:cursor_created, :cursor_id)
                ORDER BY f.created_at DESC, f.id DESC
                LIMIT :limit
                """
            )
            result = await self._db.execute(
                stmt,
                {
                    "org_id": organization_id,
                    "session_id": session_id,
                    "cursor_created": cursor_created,
                    "cursor_id": cursor_id,
                    "limit": effective_limit,
                },
            )
        else:
            stmt = sql_text(
                base_query
                + """
                ORDER BY f.created_at DESC, f.id DESC
                LIMIT :limit
                """
            )
            result = await self._db.execute(
                stmt,
                {
                    "org_id": organization_id,
                    "session_id": session_id,
                    "limit": effective_limit,
                },
            )

        rows = result.fetchall()
        has_more = len(rows) == effective_limit
        if has_more:
            rows = rows[: effective_limit - 1]

        facts = []
        for r in rows:
            facts.append(
                {
                    "id": str(r[0]),
                    "content": r[1],
                    "subject": r[2],
                    "predicate": r[3],
                    "object": r[4],
                    "confidence": float(r[5]) if r[5] is not None else 0.0,
                    "source_episode_id": str(r[6]) if r[6] else None,
                    "created_at": r[7].isoformat() if r[7] else None,
                    "subject_type": r[8],
                    "object_type": r[9],
                    "subject_entity_id": str(r[10]) if r[10] else None,
                    "object_entity_id": str(r[11]) if r[11] else None,
                }
            )

        next_cursor: str | None = None
        if has_more and facts:
            last = facts[-1]
            raw = f"{last['created_at']}|{last['id']}"
            next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()

        return facts, next_cursor

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
