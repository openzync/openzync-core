"""Retrieval repository — pgvector + ranked-query SQL for hybrid retrieval.

Owns the raw pgvector statements previously inline in
``services/hybrid_retriever.py``. Services pass resolved values
(embedding, project id, limit) only — never SQL fragments — by asking
for a built statement and executing it through ``ranked_execute``.

HNSW note: the ``ORDER BY embedding <=>`` form must stay on the raw
operator (not the SELECT alias) or the query degrades to a seq scan.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.embeddings import CANONICAL_EMBED_DIM


class RetrievalRepository:
    """All DB access for the hybrid-retrieval vector legs."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    def build_episode_vector_stmt(
        self,
        embedding: list[float],
        project_id: UUID,
        limit: int,
    ) -> TextClause:
        """Build the episode nearest-neighbour statement (no I/O).

        Args:
            embedding: Precomputed query embedding (canonical dim).
            project_id: Project scope.
            limit: Max results.

        Returns:
            The bound pgvector ``text()`` statement.
        """
        # Raw ``text()`` — the pgvector asyncpg codec encodes the native
        # ``list[float]`` directly; binding through ``Vector``'s
        # bind_processor stringified it (asyncpg DataError → 503).
        return text(
            "SELECT id, content, role, created_at, "  # noqa: S608
            "1.0 - coalesce(embedding <=> "
            f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})), 1.0) AS score "
            "FROM episodes "
            "WHERE project_id = :project_id "
            "AND is_deleted = false "
            "AND embedding IS NOT NULL "
            "AND vector_dims(embedding) > 0 "
            "ORDER BY embedding <=> "
            f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})) "
            "LIMIT :limit"
            # S608 justification: interpolates the int constant
            # CANONICAL_EMBED_DIM into a static CAST, never user input.
        ).bindparams(
            embedding=embedding,
            project_id=project_id,
            limit=limit,
        )

    def build_fact_vector_stmt(
        self,
        embedding: list[float],
        project_id: UUID,
        limit: int,
        effective_time: datetime | None = None,
    ) -> TextClause:
        """Build the fact nearest-neighbour statement (no I/O).

        Args:
            embedding: Precomputed query embedding (canonical dim).
            project_id: Project scope.
            limit: Max results.
            effective_time: Effective-at instant for the validity
                predicate (``None`` resolves to now here).

        Returns:
            The bound pgvector ``text()`` statement.
        """
        as_of = effective_time or datetime.now(UTC)
        return text(
            "SELECT id, content, subject, predicate, object, "  # noqa: S608
            "confidence, created_at, valid_from, valid_to, invalid_at, "
            "1.0 - coalesce(embedding <=> "
            f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})), 1.0) AS score "
            "FROM facts "
            "WHERE project_id = :project_id "
            "AND (invalid_at IS NULL OR invalid_at > :effective_time) "
            "AND (valid_from IS NULL OR valid_from <= :effective_time) "
            "AND (valid_to IS NULL OR valid_to > :effective_time) "
            "AND embedding IS NOT NULL "
            "AND vector_dims(embedding) > 0 "
            "ORDER BY embedding <=> "
            f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})) "
            "LIMIT :limit"
            # S608 justification: interpolates the int constant
            # CANONICAL_EMBED_DIM into a static CAST, never user input.
        ).bindparams(
            embedding=embedding,
            project_id=project_id,
            effective_time=as_of,
            limit=limit,
        )

    async def vector_search_episodes(
        self,
        embedding: list[float],
        project_id: UUID,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Find nearest-neighbour episodes by cosine distance.

        Args:
            embedding: Precomputed query embedding (canonical dim).
            project_id: Project scope.
            limit: Max results.

        Returns:
            Result dicts with ``id``, ``content``, ``role``, ``score``,
            and ``created_at`` keys.
        """
        stmt = self.build_episode_vector_stmt(embedding, project_id, limit)
        return await self.ranked_execute(stmt)

    async def vector_search_facts(
        self,
        embedding: list[float],
        project_id: UUID,
        limit: int,
        effective_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Find nearest-neighbour facts valid at ``effective_time``.

        Args:
            embedding: Precomputed query embedding (canonical dim).
            project_id: Project scope.
            limit: Max results.
            effective_time: Effective-at instant for the validity
                predicate (``None`` resolves to now here).

        Returns:
            Result dicts with ``id``, ``content``, ``subject``,
            ``predicate``, ``object``, ``score``, ``confidence``,
            ``valid_from``, ``valid_to``, and ``invalid_at`` keys.
        """
        stmt = self.build_fact_vector_stmt(
            embedding, project_id, limit, effective_time
        )
        return await self.ranked_execute(stmt)

    async def ranked_execute(
        self,
        stmt: Select[Any],
    ) -> list[dict[str, Any]]:
        """Execute a ranked SELECT and convert rows to dicts.

        Maps the column names from SQLAlchemy result rows to a consistent
        dict format. The ``score`` column is rounded to 6 decimal places.

        Args:
            stmt: A SELECT statement with a ``score`` label on the
                relevance column.

        Returns:
            A list of dicts with column names as keys.
        """
        result = await self._db.execute(stmt)
        rows = result.all()
        output: list[dict[str, Any]] = []
        for row in rows:
            row_dict = dict(row._mapping)  # type: ignore[attr-defined]
            if "score" in row_dict and row_dict["score"] is not None:
                row_dict["score"] = round(float(row_dict["score"]), 6)
            output.append(row_dict)
        return output
