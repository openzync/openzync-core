"""Vector search — query embedding plus pgvector legs (delegating to the repo)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import SearchLegFailedError
from repositories.retrieval_repository import RetrievalRepository

logger = logging.getLogger(__name__)


class VectorSearchMixin:
    """Embedding generation and vector legs; SQL lives in the repository."""

    _db: AsyncSession
    _last_query_embedding_dim: int | None

    async def _embed_query(
        self,
        query: str,
        project_id: UUID | None = None,  # noqa: ARG002
    ) -> list[float]:
        """Generate an embedding vector for a search query.

        Embeds with the single local embedder
        (``core.embeddings.embed_query``) — the *query* prefix, not
        ``embed_passage``. The two are not interchangeable: ``nomic-embed-
        text-v1.5`` is trained with asymmetric search/document prefixes and
        mixing them degrades recall silently (still 768 valid floats).
        Rejects any vector that is not exactly ``CANONICAL_EMBED_DIM`` —
        fail loud, never search with a wrong-dim vector.

        Args:
            query: Natural-language query text.
            project_id: Unused; retained for call-site compatibility.

        Returns:
            A list of floats representing the query embedding.

        Raises:
            SearchLegFailedError: If embedding generation fails, returns
                no embeddings, or returns a non-canonical-dim vector.
        """
        try:
            from core.embeddings import embed_query, validate_embedding_dim

            vectors = await embed_query([query])
            if vectors:
                query_embedding = vectors[0]
                validate_embedding_dim(
                    query_embedding, source="hybrid_retriever._embed_query"
                )
                self._last_query_embedding_dim = len(query_embedding)
                return query_embedding
            raise SearchLegFailedError(
                leg_name="embedding",
                original_error="Embedding response contained no embeddings.",
            )
        except SearchLegFailedError:
            raise
        except Exception as exc:
            logger.error(
                "hybrid_retriever.embed_query_failed",
                extra={"query": query[:100], "leg": "embedding"},
                exc_info=True,
            )
            raise SearchLegFailedError(
                leg_name="embedding", original_error=str(exc)
            ) from exc

    async def _vector_search_episodes(
        self,
        query_embedding: list[float],
        project_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Semantic search over episodes (cosine distance, HNSW-ordered).

        The statement is built by the repository from
        ``(embedding, project_id, limit)`` only; execution runs through
        the ``_execute_ranked_query`` seam. The query embedding is
        generated once upstream in ``hybrid_search`` and shared across
        both vector legs.

        Args:
            query_embedding: Precomputed embedding vector for the query.
            project_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``role``,
            ``score``, and ``created_at`` keys.
        """
        repo = RetrievalRepository(self._db)
        stmt = repo.build_episode_vector_stmt(
            query_embedding, project_id, limit
        )
        results = await self._execute_ranked_query(stmt)
        if results:
            logger.debug(
                "hybrid_retriever.episode_vector_success",
                extra={"result_count": len(results)},
            )
        return results

    async def _vector_search_facts(
        self,
        query_embedding: list[float],
        project_id: UUID,
        limit: int = 20,
        query_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search over facts valid at ``query_time``.

        The statement is built by the repository from
        ``(embedding, project_id, limit)`` only; execution runs through
        the ``_execute_ranked_query`` seam. Same supersession semantics
        as the fact BM25 leg (superseded and not-yet-valid rows never
        leak into results).

        Args:
            query_embedding: Precomputed embedding vector for the query.
            project_id: Scoped user UUID.
            limit: Max results.
            query_time: Effective-at instant for the validity predicate.

        Returns:
            A list of result dicts with ``id``, ``content``, ``subject``,
            ``predicate``, ``object``, ``score``, ``confidence``,
            ``valid_from``, ``valid_to``, and ``invalid_at`` keys.
        """
        repo = RetrievalRepository(self._db)
        stmt = repo.build_fact_vector_stmt(
            query_embedding, project_id, limit, query_time
        )
        results = await self._execute_ranked_query(stmt)
        if results:
            logger.debug(
                "hybrid_retriever.fact_vector_success",
                extra={"result_count": len(results)},
            )
        return results

    async def _execute_ranked_query(
        self,
        stmt: Select[Any],
    ) -> list[dict[str, Any]]:
        """Execute a ranked SELECT and convert rows to dicts.

        Thin seam over :meth:`RetrievalRepository.ranked_execute` (kept
        so existing callers mock one place).

        Args:
            stmt: A SQLAlchemy ``select()`` statement with a ``score``
                label on the relevance column.

        Returns:
            A list of dicts with column names as keys.
        """
        return await RetrievalRepository(self._db).ranked_execute(stmt)
