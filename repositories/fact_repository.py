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

from typing import Any, Literal

from core.cursor import decode_cursor, encode_cursor
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
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
        project_id: UUID,
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
            user_id: FK to the user who created the fact.
            organization_id: Denormalized org ID for RLS.
            project_id: Denormalized project ID for project isolation.
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
            project_id=project_id,
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
            embedding=None,
        )
        self._db.add(fact)
        await self._db.flush()
        await self._db.refresh(fact)
        return fact

    async def create_or_skip(
        self,
        user_id: UUID,
        organization_id: UUID,
        project_id: UUID,
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
    ) -> Fact | None:
        """Insert a fact, skipping silently if the triple already exists
        for this episode (exclusion constraint ``uq_facts_temporal_excl``).

        Uses PostgreSQL ``INSERT ... ON CONFLICT DO NOTHING`` with
        ``RETURNING`` — returns ``None`` when the conflict fires so the
        caller can log a duplicate skip without crashing the transaction.

        Args:
            Same as :meth:`create` plus ``project_id``.

        Returns:
            The newly created :class:`Fact` instance, or ``None`` if a
            duplicate was skipped.
        """
        from sqlalchemy import func, insert

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(Fact)
            .values(
                id=func.gen_random_uuid(),
                user_id=user_id,
                organization_id=organization_id,
                project_id=project_id,
                content=content,
                subject=subject,
                predicate=predicate,
                object=obj,
                subject_type=subject_type,
                object_type=object_type,
                confidence=confidence,
                source_episode_id=source_episode_id,
                valid_from=valid_from or datetime.now(),
                subject_entity_id=subject_entity_id,
                object_entity_id=object_entity_id,
            embedding=None,
            )
            .on_conflict_do_nothing(
                constraint="uq_facts_temporal_excl",
            )
            .returning(Fact)
        )
        result = await self._db.execute(stmt)
        await self._db.flush()
        return result.scalar_one_or_none()

    # ── Batch Create ────────────────────────────────────────────────────────────

    async def batch_create(
        self,
        organization_id: UUID,
        project_id: UUID,
        user_id: UUID,
        facts: list[dict],
        on_conflict: Literal["error", "skip"] = "error",
    ) -> list[Fact]:
        """Bulk-insert facts using a single INSERT statement.

        More efficient than per-row ``create()`` for batch ingestion.
        Uses SQLAlchemy's ``insert()`` or PostgreSQL's
        ``INSERT…ON CONFLICT`` with ``returning()`` to fetch generated IDs.

        Args:
            organization_id: Denormalized org ID for RLS.
            project_id: Denormalized project ID for project isolation.
            user_id: FK to the user who created the facts.
            facts: List of dicts, each with optional keys: ``subject``,
                ``predicate``, ``object``, ``content``, ``confidence``,
                ``source_episode_id``, ``valid_from``, ``valid_to``.
            on_conflict: What to do when a row violates the temporal
                exclusion constraint ``uq_facts_temporal_excl``.
                ``"error"`` (default) — let the exclusion constraint raise
                ``IntegrityError``, propagating to the global 409 handler.
                ``"skip"`` — use ``ON CONFLICT DO NOTHING``; conflicting
                rows are silently omitted from the result.

        Returns:
            A list of created ``Fact`` ORM instances with server-generated
            fields populated.  When ``on_conflict="skip"``, only
            non-conflicting rows are returned.
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
                    "project_id": project_id,
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
                    "embedding": None,
                }
            )

        # ── Conflict strategy ────────────────────────────────────────────
        if on_conflict == "skip":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = (
                pg_insert(Fact)
                .on_conflict_do_nothing(constraint="uq_facts_temporal_excl")
                .returning(Fact)
            )
        else:
            from sqlalchemy import insert

            stmt = insert(Fact).returning(Fact)

        try:
            result = await self._db.execute(stmt, rows)
        except IntegrityError:
            logger.warning(
                "fact_repository.exclusion_conflict",
                extra={
                    "input_count": len(facts),
                    "user_id": str(user_id),
                    "project_id": str(project_id),
                    "organization_id": str(organization_id),
                },
            )
            raise

        await self._db.flush()

        created = list(result.scalars().all())

        # Refresh each to populate server defaults (created_at, etc.)
        for fact in created:
            await self._db.refresh(fact)

        skipped = len(facts) - len(created)

        logger.info(
            "fact_repository.batch_created",
            extra={
                "count": len(created),
                "skipped_count": skipped,
                "user_id": str(user_id),
                "project_id": str(project_id),
                "organization_id": str(organization_id),
                "on_conflict": on_conflict,
            },
        )

        return created

    async def batch_create_or_skip(
        self,
        organization_id: UUID,
        project_id: UUID,
        user_id: UUID,
        source_episode_id: UUID,
        facts: list[dict[str, Any]],
    ) -> list[Fact]:
        """Batch-insert facts with ``ON CONFLICT DO NOTHING``.

        More efficient than looping over ``create_or_skip`` — uses a
        single ``INSERT ... ON CONFLICT DO NOTHING RETURNING *``
        statement.

        Only newly inserted rows are returned; conflicting rows are
        silently skipped.

        Args:
            organization_id: Denormalized org ID for RLS.
            project_id: Denormalized project ID for project isolation.
            user_id: FK to the user who created the facts.
            source_episode_id: FK back to the source episode.
            facts: List of fact dicts with keys: ``subject``, ``predicate``,
                ``object``, ``confidence``, ``subject_type``, ``object_type``,
                ``subject_entity_id``, ``object_entity_id``.

        Returns:
            List of newly created :class:`Fact` ORM instances (conflicting
            rows are excluded).
        """
        # ⚠️ Uses constraint name rather than index_elements to stay
        # consistent with the existing create_or_skip method. Both must
        # reference the same exclusion constraint for correct dedup.
        from datetime import datetime, timezone

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        now = datetime.now(timezone.utc)
        rows = [
            {
                "user_id": user_id,
                "organization_id": organization_id,
                "project_id": project_id,
                "content": f"{f['subject']} {f['predicate']} {f['object']}",
                "subject": f["subject"],
                "predicate": f["predicate"],
                "object": f["object"],
                "subject_type": f.get("subject_type", "literal"),
                "object_type": f.get("object_type", "literal"),
                "confidence": f["confidence"],
                "source_episode_id": source_episode_id,
                "valid_from": now,
                "subject_entity_id": f.get("subject_entity_id"),
                "object_entity_id": f.get("object_entity_id"),
                "embedding": None,
            }
            for f in facts
        ]

        stmt = (
            pg_insert(Fact)
            .on_conflict_do_nothing(constraint="uq_facts_temporal_excl")
            .returning(Fact)
        )
        result = await self._db.execute(stmt, rows)
        await self._db.flush()

        created = list(result.scalars().all())

        logger.info(
            "fact_repository.batch_create_or_skip",
            extra={
                "input_count": len(facts),
                "created_count": len(created),
            },
        )

        return created

    # ── Temporal Queries ───────────────────────────────────────────────────────

    async def get_all_active_for_project(
        self,
        project_id: UUID,
        *,
        organization_id: UUID | None = None,
    ) -> list[Fact]:
        """Return all non-invalidated facts for a project.

        Used by the temporal validation service to scan for cross-episode
        overlaps and invalid ranges.

        Args:
            project_id: The project to fetch facts for.
            organization_id: Optional tenant filter for defense-in-depth.

        Returns:
            List of active ``Fact`` ORM instances (``invalid_at IS NULL``).
        """
        from sqlalchemy import select

        stmt = (
            select(Fact)
            .where(Fact.project_id == project_id)
            .where(Fact.invalid_at.is_(None))
            .order_by(Fact.valid_from.asc().nullsfirst())
        )
        if organization_id is not None:
            stmt = stmt.where(Fact.organization_id == organization_id)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_facts_at_time(
        self,
        project_id: UUID,
        timestamp: datetime,
        *,
        organization_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Fact]:
        """Return non-invalidated facts valid at a specific point in time.

        Uses the btree index ``ix_fact_user_valid_range`` on
        ``(user_id, valid_from, valid_to)`` — **not** the GiST exclusion
        constraint index.

        Query pattern::

            WHERE project_id = :project_id
              AND invalid_at IS NULL
              AND (valid_from IS NULL OR valid_from <= :timestamp)
              AND (valid_to IS NULL OR valid_to > :timestamp)
            ORDER BY valid_from DESC NULLS LAST
            LIMIT :limit OFFSET :offset

        Args:
            project_id: Project scope.
            timestamp: Point in time to query.  Facts whose valid range
                contains this timestamp are returned.
            organization_id: Optional tenant filter for defense-in-depth.
            limit: Maximum number of results to return (capped at 200).
            offset: Number of results to skip (for pagination).

        Returns:
            A list of ``Fact`` ORM instances valid at ``timestamp``.
        """
        from sqlalchemy import select

        effective_limit = min(limit, 200)

        stmt = (
            select(Fact)
            .where(Fact.project_id == project_id)
            .where(Fact.invalid_at.is_(None))
            .where(
                (Fact.valid_from.is_(None)) | (Fact.valid_from <= timestamp),
            )
            .where(
                (Fact.valid_to.is_(None)) | (Fact.valid_to > timestamp),
            )
            .order_by(Fact.valid_from.desc().nullslast())
            .limit(effective_limit)
            .offset(offset)
        )
        if organization_id is not None:
            stmt = stmt.where(Fact.organization_id == organization_id)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_facts_in_range(
        self,
        project_id: UUID,
        start: datetime,
        end: datetime,
        *,
        organization_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Fact]:
        """Return non-invalidated facts whose valid range overlaps ``[start, end)``.

        Btree-backed query (NOT GiST ``&&``)::

            WHERE project_id = :project_id
              AND invalid_at IS NULL
              AND (valid_from IS NULL OR valid_from < :end)
              AND (valid_to IS NULL OR valid_to > :start)
            ORDER BY valid_from ASC
            LIMIT :limit OFFSET :offset

        The GiST range-overlap index is intentionally deferred — add it
        only if profiling proves the btree plan is too slow at scale.

        Args:
            project_id: Project scope.
            start: Start of the query range (inclusive per ``'[)'``
                semantics).
            end: End of the query range (exclusive per ``'[)'`` semantics).
            organization_id: Optional tenant filter for defense-in-depth.
            limit: Maximum number of results to return (capped at 200).
            offset: Number of results to skip (for pagination).

        Returns:
            A list of ``Fact`` ORM instances whose valid range overlaps
            the query range.
        """
        from sqlalchemy import select

        effective_limit = min(limit, 200)

        stmt = (
            select(Fact)
            .where(Fact.project_id == project_id)
            .where(Fact.invalid_at.is_(None))
            .where(
                (Fact.valid_from.is_(None)) | (Fact.valid_from < end),
            )
            .where(
                (Fact.valid_to.is_(None)) | (Fact.valid_to > start),
            )
            .order_by(Fact.valid_from.asc().nullsfirst())
            .limit(effective_limit)
            .offset(offset)
        )
        if organization_id is not None:
            stmt = stmt.where(Fact.organization_id == organization_id)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    # ── Soft Delete by User ──────────────────────────────────────────────────

    async def soft_delete_by_project(self, project_id: UUID) -> int:
        """Soft-delete all facts for a project by setting ``invalid_at``.

        Uses the ORM ``update()`` construct rather than raw SQL so that
        SQLAlchemy's column-level ``onupdate`` hook fires for
        ``updated_at`` on each row.

        Args:
            project_id: The project's UUID.

        Returns:
            Number of facts invalidated.
        """
        now = datetime.now()
        result = await self._db.execute(
            update(Fact)
            .where(Fact.project_id == project_id)
            .where(Fact.invalid_at.is_(None))
            .values(invalid_at=now, updated_at=now)
        )
        await self._db.flush()
        return result.rowcount  # type: ignore[return-value]

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
        from sqlalchemy import select, text as sql_text

        effective_limit = min(limit, 200) + 1  # +1 to detect has_more

        # Decode cursor
        cursor_created: datetime | None = None
        cursor_id: UUID | None = None
        if cursor:
            try:
                decoded = decode_cursor(cursor)
                parts = decoded.split("|")
                if len(parts) == 2:
                    cursor_created = datetime.fromisoformat(parts[0])
                    cursor_id = UUID(parts[1])
            except (ValueError, TypeError):
                logger.warning(
                    "fact_repository.invalid_cursor",
                    extra={"cursor": cursor},
                )

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
            next_cursor = encode_cursor(raw)

        return facts, next_cursor

    # ── Vector Search ─────────────────────────────────────────────────────────

    async def search_by_vector(
        self, embedding: list[float], project_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Search facts by vector similarity (pgvector cosine distance).

        Uses the ``<=>`` operator on the ``embedding`` column. The score is
        inverted (``1 - distance``) so higher = more similar. Only returns
        facts that have a non-null embedding.

        Args:
            embedding: The query embedding vector.
            project_id: Scope results to this project.
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
                WHERE project_id = :project_id
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> :embedding
                LIMIT :limit
                """
            ),
            {"embedding": embedding, "project_id": project_id, "limit": effective_limit},
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
        self, query: str, project_id: UUID, org_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Search facts by BM25 full-text (PostgreSQL ``ts_rank``).

        Tokenises the query via ``plainto_tsquery`` and ranks results using
        ``ts_rank`` over an English text search configuration on the
        ``content`` column.

        Args:
            query: Raw search text (no special syntax needed).
            project_id: Scope results to this project.
            org_id: Tenant scope for multi-tenant isolation.
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
                WHERE project_id = :project_id
                  AND organization_id = :org_id
                  AND to_tsvector('english', content)
                      @@ plainto_tsquery('english', :query)
                ORDER BY score DESC
                LIMIT :limit
                """
            ),
            {"query": query, "project_id": project_id, "org_id": org_id, "limit": effective_limit},
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
