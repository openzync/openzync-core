"""Hybrid retrieval — vector + BM25 + graph BFS with RRF fusion.

Combines three retrieval strategies and merges results using Reciprocal
Rank Fusion (RRF) for robust context retrieval:

1. **Vector search** (pgvector cosine similarity) — semantic matching.
2. **BM25 search** (PostgreSQL ``ts_rank``) — keyword / lexical matching.
3. **Graph BFS** (PostgreSQL recursive CTE) — entity-relationship traversal.

The RRF formula is: ``score(d) = Σ 1 / (60 + rank_s(d))`` across
all three sources.  Results are deduplicated by source ID and the
top-N by merged score are returned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Float, Select, cast, func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import SearchLegFailedError
from middleware.metrics import graph_search_latency_seconds, reranker_latency_seconds
from models.episode import Episode
from models.fact import Fact

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend
    from packages.reranker import CrossEncoderReranker
    from schemas.organization_config import OrgConfigBase

logger = logging.getLogger(__name__)

from packages.reranker import DEFAULT_RERANK_TOP_K, DEFAULT_RERANK_TOP_N

# ── Constants ──────────────────────────────────────────────────────────────────

RRF_K: int = 60
"""RRF constant — controls how quickly rank contribution decays."""

MAX_BFS_RESULTS: int = 50
"""Max results from the graph BFS leg before RRF merging."""


class HybridRetriever:
    """Combines vector, BM25, and graph retrieval with RRF fusion.

    Args:
        db: An async SQLAlchemy session (request-scoped).
        org_id: The authenticated organization UUID for tenant isolation.
        redis: Optional async Redis client for result caching.
    """

    def __init__(
        self,
        db: AsyncSession,
        org_id: UUID,
        redis: object | None = None,
        graph_backends: list[GraphBackend] | None = None,
        org_config: OrgConfigBase | None = None,
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        self._db = db
        self._org_id = org_id
        self._redis = redis
        self._graph_backends = graph_backends or []
        self._org_config = org_config
        self._reranker = reranker
        self._rerank_top_k: int = (
            org_config.reranker_top_k if org_config and org_config.reranker_top_k else DEFAULT_RERANK_TOP_K
        )
        self._rerank_top_n: int = (
            org_config.reranker_top_n if org_config and org_config.reranker_top_n else DEFAULT_RERANK_TOP_N
        )

    # ── Public API ──────────────────────────────────────────────────────────────

    async def hybrid_search(
        self,
        query: str,
        project_id: UUID,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Run hybrid search across all sources and return RRF-merged results.

        Orchestrates five retrieval legs concurrently:

        - Episodes (vector + BM25)
        - Facts (vector + BM25)
        - Graph entities (BFS from entities matching the query)

        All legs run concurrently.  If any single leg fails, the entire
        search fails with a ``SearchLegFailedError`` — no silent partial
        results are returned.

        Results are grouped by type in the return dict with source counts.

        Args:
            query: The natural-language search query.
            project_id: Scoped user UUID.
            limit: Max items per source type before RRF merge.

        Returns:
            A dict with:
            - ``episodes``: RRF-merged episode results.
            - ``facts``: RRF-merged fact results.
            - ``entities``: Graph entity results from BFS.
            - ``communities``: Community summaries (empty if unavailable).
            - ``source_counts``: Item count per source type.
            - ``total_items``: Sum of all items across sources.

        Raises:
            SearchLegFailedError: If any retrieval leg (vector, BM25,
                graph BFS, or reranker) fails.
        """
        _search_start = time.monotonic()

        # ── Run all three retrieval legs concurrently ──────────────────────
        # Each leg returns a list of dicts with at minimum ``id`` and
        # ``score`` keys for RRF merging.
        episode_vector_results: list[dict[str, Any]] = []
        episode_bm25_results: list[dict[str, Any]] = []
        fact_vector_results: list[dict[str, Any]] = []
        fact_bm25_results: list[dict[str, Any]] = []
        entity_results: list[dict[str, Any]] = []

        retrieval_limit = max(limit, self._rerank_top_k) if self._reranker is not None else limit

        # Vector search for episodes and facts
        try:
            episode_vector_results = await self._vector_search_episodes(
                query, project_id, retrieval_limit
            )
        except Exception as exc:
            logger.error(
                "hybrid_retriever.episode_vector_failed",
                extra={"project_id": str(project_id), "query": query, "leg": "episode_vector"},
                exc_info=True,
            )
            await self._db.rollback()
            raise SearchLegFailedError(leg_name="episode_vector", original_error=str(exc)) from exc

        try:
            fact_vector_results = await self._vector_search_facts(
                query, project_id, retrieval_limit
            )
        except Exception as exc:
            logger.error(
                "hybrid_retriever.fact_vector_failed",
                extra={"project_id": str(project_id), "query": query, "leg": "fact_vector"},
                exc_info=True,
            )
            await self._db.rollback()
            raise SearchLegFailedError(leg_name="fact_vector", original_error=str(exc)) from exc

        # BM25 search for episodes and facts
        try:
            episode_bm25_results = await self._bm25_search_episodes(
                query, project_id, retrieval_limit
            )
        except Exception as exc:
            logger.error(
                "hybrid_retriever.episode_bm25_failed",
                extra={"project_id": str(project_id), "query": query, "leg": "episode_bm25"},
                exc_info=True,
            )
            await self._db.rollback()
            raise SearchLegFailedError(leg_name="episode_bm25", original_error=str(exc)) from exc

        try:
            fact_bm25_results = await self._bm25_search_facts(query, project_id, retrieval_limit)
        except Exception as exc:
            logger.error(
                "hybrid_retriever.fact_bm25_failed",
                extra={"project_id": str(project_id), "query": query, "leg": "fact_bm25"},
                exc_info=True,
            )
            await self._db.rollback()
            raise SearchLegFailedError(leg_name="fact_bm25", original_error=str(exc)) from exc

        # Graph BFS via entity name search
        try:
            entity_results = await self._graph_bfs_search(query, project_id)
        except SearchLegFailedError:
            raise
        except Exception as exc:
            logger.error(
                "hybrid_retriever.graph_bfs_failed",
                extra={"project_id": str(project_id), "query": query, "leg": "graph_bfs"},
                exc_info=True,
            )
            await self._db.rollback()
            raise SearchLegFailedError(leg_name="graph_bfs", original_error=str(exc)) from exc

        # ── RRF merge per type ────────────────────────────────────────────
        # Use rerank_top_k as the RRF candidate pool size when re-ranking is active
        rrf_top_n = self._rerank_top_k if self._reranker is not None else limit

        merged_episodes = self._rrf_merge(
            [episode_vector_results, episode_bm25_results],
            top_n=rrf_top_n,
        )

        merged_facts = self._rrf_merge(
            [fact_vector_results, fact_bm25_results],
            top_n=rrf_top_n,
        )

        # Entities: BFS results directly (single source, no merge needed)
        entities = entity_results[:limit]

        # ── Re-ranking step ───────────────────────────────────────────────
        if self._reranker is not None:
            _rerank_start = time.monotonic()
            try:
                merged_episodes = await self._reranker.rerank(
                    query, merged_episodes, top_n=self._rerank_top_n,
                )
                merged_facts = await self._reranker.rerank(
                    query, merged_facts, top_n=self._rerank_top_n,
                )
                _rerank_elapsed = time.monotonic() - _rerank_start
                reranker_latency_seconds.labels(
                    backend=self._reranker.backend_name,
                ).observe(_rerank_elapsed)
            except Exception as exc:
                logger.error(
                    "hybrid_retriever.rerank_failed",
                    extra={
                        "query": query[:100],
                        "duration_ms": round((time.monotonic() - _rerank_start) * 1000),
                        "leg": "reranker",
                    },
                    exc_info=True,
                )
                raise SearchLegFailedError(leg_name="reranker", original_error=str(exc)) from exc

        graph_search_latency_seconds.observe(time.monotonic() - _search_start)

        return {
            "episodes": merged_episodes,
            "facts": merged_facts,
            "entities": entities,
            "communities": [],  # Placeholder — community detection TBD
            "source_counts": {
                "episodes": {
                    "vector": len(episode_vector_results),
                    "bm25": len(episode_bm25_results),
                    "merged": len(merged_episodes),
                },
                "facts": {
                    "vector": len(fact_vector_results),
                    "bm25": len(fact_bm25_results),
                    "merged": len(merged_facts),
                },
                "entities": {
                    "graph_bfs": len(entity_results),
                },
            },
            "total_items": len(merged_episodes) + len(merged_facts) + len(entities),
        }

    # ── Vector Search ──────────────────────────────────────────────────────────

    async def _embed_query(self, query: str) -> list[float]:
        """Generate an embedding vector for a search query.

        Uses the configured LLM backend's embedding model.

        Args:
            query: Natural-language query text.

        Returns:
            A list of floats representing the query embedding.

        Raises:
            SearchLegFailedError: If embedding generation fails or returns
                no embeddings.
        """
        try:
            from core.llm import resolve_backend

            org_config_dict = (
                self._org_config.to_llm_config_dict() if self._org_config else None
            )
            backend = await resolve_backend(
                provider=self._org_config.embedding_backend
                if self._org_config
                else None,
                org_config=org_config_dict,
            )
            response = await backend.embed([query])
            if response.embeddings and len(response.embeddings) > 0:
                return response.embeddings[0]
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
            raise SearchLegFailedError(leg_name="embedding", original_error=str(exc)) from exc

    async def _vector_search_episodes(
        self,
        query: str,
        project_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Semantic search over episodes using pgvector cosine similarity.

        Generates an embedding for the query, then finds the nearest
        neighbours in ``episodes.embedding`` using the ``<=>`` (cosine
        distance) operator.

        The ``embedding`` column is ``vector(768)`` — cast via
        :class:`pgvector.sqlalchemy.Vector` at query time.

        Args:
            query: Natural-language query text.
            project_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``role``,
            ``score``, and ``created_at`` keys.

        Raises:
            SearchLegFailedError: If embedding generation fails.
        """
        query_embedding = await self._embed_query(query)

        dim: int = (
            self._org_config.embedding_dim
            if self._org_config and self._org_config.embedding_dim
            else 1536
        )

        from pgvector.sqlalchemy import (
            Vector,  # lazy: numpy CPU compat; caught by outer try/except
        )

        vector_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
        embedding_col = cast(Episode.embedding, Vector(dim))
        query_literal = literal_column(f"'{vector_str}'::vector({dim})")

        stmt = (
            select(
                Episode.id,
                Episode.content,
                Episode.role,
                Episode.created_at,
                (
                    literal(1.0, Float)
                    - func.coalesce(
                        embedding_col.op("<=>")(query_literal),
                        literal(1.0, Float),
                    )
                ).label("score"),
            )
            .where(
                Episode.project_id == project_id,
                Episode.is_deleted.is_(False),
                Episode.embedding.isnot(None),
                func.cardinality(Episode.embedding) > 0,
            )
            .order_by(text("score DESC"))
            .limit(limit)
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
        query: str,
        project_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Semantic search over facts using pgvector cosine similarity.

        Same pattern as ``_vector_search_episodes`` but operates on the
        ``facts.embedding`` column.

        Args:
            query: Natural-language query text.
            project_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``subject``,
            ``predicate``, ``object``, ``score``, and ``confidence`` keys.

        Raises:
            SearchLegFailedError: If embedding generation fails.
        """
        query_embedding = await self._embed_query(query)

        # Resolve embedding dimension from org config so the runtime
        # ``::vector(N)`` cast matches the model that produced the data.
        # Defaults to 1536 (text-embedding-3-small) when not configured.
        dim: int = (
            self._org_config.embedding_dim
            if self._org_config and self._org_config.embedding_dim
            else 1536
        )

        from pgvector.sqlalchemy import (
            Vector,  # lazy: numpy CPU compat; caught by outer try/except
        )

        vector_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
        embedding_col = cast(Fact.embedding, Vector(dim))
        query_literal = literal_column(f"'{vector_str}'::vector({dim})")

        stmt = (
            select(
                Fact.id,
                Fact.content,
                Fact.subject,
                Fact.predicate,
                Fact.object,
                Fact.confidence,
                Fact.created_at,
                (
                    literal(1.0, Float)
                    - func.coalesce(
                        embedding_col.op("<=>")(query_literal),
                        literal(1.0, Float),
                    )
                ).label("score"),
            )
            .where(
                Fact.project_id == project_id,
                Fact.invalid_at.is_(None),
                Fact.embedding.isnot(None),
                func.cardinality(Fact.embedding) > 0,
            )
            .order_by(text("score DESC"))
            .limit(limit)
        )
        results = await self._execute_ranked_query(stmt)
        if results:
            logger.debug(
                "hybrid_retriever.fact_vector_success",
                extra={"result_count": len(results)},
            )
        return results

    # ── BM25 Search ────────────────────────────────────────────────────────────

    async def _bm25_search_episodes(
        self,
        query: str,
        project_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Keyword search over episodes using PostgreSQL full-text search.

        Uses ``plainto_tsquery`` for user-friendly query parsing and
        ``ts_rank`` for BM25-like relevance scoring.

        Args:
            query: Keyword query string.
            project_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``role``,
            ``score``, and ``created_at`` keys.
        """
        ts_query = func.plainto_tsquery("english", query)
        stmt = (
            select(
                Episode.id,
                Episode.content,
                Episode.role,
                Episode.created_at,
                func.ts_rank(
                    func.to_tsvector("english", Episode.content),
                    ts_query,
                ).label("score"),
            )
            .where(
                Episode.project_id == project_id,
                Episode.is_deleted.is_(False),
                func.to_tsvector("english", Episode.content).op("@@")(ts_query),
            )
            .order_by(text("score DESC"))
            .limit(limit)
        )
        return await self._execute_ranked_query(stmt)

    async def _bm25_search_facts(
        self,
        query: str,
        project_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Keyword search over facts using PostgreSQL full-text search.

        Args:
            query: Keyword query string.
            project_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``subject``,
            ``predicate``, ``object``, ``score``, and ``confidence`` keys.
        """
        ts_query = func.plainto_tsquery("english", query)
        stmt = (
            select(
                Fact.id,
                Fact.content,
                Fact.subject,
                Fact.predicate,
                Fact.object,
                Fact.confidence,
                Fact.created_at,
                func.ts_rank(
                    func.to_tsvector("english", Fact.content),
                    ts_query,
                ).label("score"),
            )
            .where(
                Fact.project_id == project_id,
                Fact.invalid_at.is_(None),
                func.to_tsvector("english", Fact.content).op("@@")(ts_query),
            )
            .order_by(text("score DESC"))
            .limit(limit)
        )
        return await self._execute_ranked_query(stmt)

    # ── Graph BFS Search ───────────────────────────────────────────────────────

    async def _graph_bfs_search(
        self,
        query: str,
        project_id: UUID,
    ) -> list[dict[str, Any]]:
        """BFS traversal from entities across all configured graph backends.

        Runs ``retrieve_graph`` on every registered backend in parallel,
        then merges results deduplicating by entity ``id``.

        Each backend independently searches for entities matching the
        query text, BFS-traverses from those entities, and returns shaped
        results.  Merging favours lower ``distance`` (closest match first).

        Args:
            query: Natural-language query for entity matching.
            project_id: Scoped user UUID.

        Returns:
            A list of entity dicts with ``id``, ``name``, ``type``,
            ``summary``, and ``distance`` keys, deduplicated and sorted
            by distance ascending.

        Raises:
            SearchLegFailedError: If any graph backend call fails.
        """
        if not self._graph_backends:
            logger.debug(
                "hybrid_retriever.graph_bfs_unavailable",
                extra={
                    "query": query,
                    "hint": "No graph backends configured for this project.",
                },
            )
            return []

        # Run each backend's retrieve_graph in parallel — failure in any
        # backend propagates immediately (no silent degradation).
        results = await asyncio.gather(
            *[
                backend.retrieve_graph(
                    org_id=self._org_id,
                    project_id=project_id,
                    query=query,
                )
                for backend in self._graph_backends
            ],
        )

        # Merge: deduplicate by id, keep first occurrence (lower distance wins)
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for r in results:
            for item in r:
                item_id = item["id"]
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    merged.append(item)

        # Sort by distance (closest first), limit to MAX_BFS_RESULTS
        merged.sort(key=lambda x: x["distance"])
        return merged[:MAX_BFS_RESULTS]

    # ── RRF Merge ──────────────────────────────────────────────────────────────

    @staticmethod
    def _rrf_merge(
        ranked_lists: list[list[dict[str, Any]]],
        top_n: int = 20,
    ) -> list[dict[str, Any]]:
        """Merge multiple ranked result lists using Reciprocal Rank Fusion.

        RRF formula:
        ``score(d) = Σ 1 / (RRF_K + rank_s(d))``

        where ``rank_s(d)`` is the 1-based rank of document ``d`` in
        source ``s``, and ``RRF_K`` is the constant (default 60).

        Results are deduplicated by ``id`` (takes the first occurrence's
        rank from each source list).

        Args:
            ranked_lists: One or more ranked result lists, each ordered
                by descending relevance.
            top_n: Maximum number of results to return after merge.

        Returns:
            Top-N results sorted by descending RRF score, each including
            an ``rrf_score`` key with the fused score.
        """
        # Accumulate RRF scores keyed by result ID
        score_map: dict[str, dict[str, Any]] = {}
        # Track rank per source for each document
        rank_map: dict[str, list[int]] = {}

        for ranked_list in ranked_lists:
            for rank, item in enumerate(ranked_list, start=1):
                item_id = str(item["id"])
                if not item_id:
                    continue

                if item_id not in score_map:
                    score_map[item_id] = dict(item)
                    rank_map[item_id] = []

                rank_map[item_id].append(rank)

        # Compute RRF scores
        for item_id, ranks in rank_map.items():
            rrf_score = sum(1.0 / (RRF_K + r) for r in ranks)
            score_map[item_id]["rrf_score"] = round(rrf_score, 6)

        # Sort by descending RRF score and take top_n
        sorted_items = sorted(
            score_map.values(),
            key=lambda x: x.get("rrf_score", 0.0),
            reverse=True,
        )

        return sorted_items[:top_n]

    # ── Query Helper ───────────────────────────────────────────────────────────

    async def _execute_ranked_query(
        self,
        stmt: Select[Any],
    ) -> list[dict[str, Any]]:
        """Execute a ranked SELECT and convert rows to dicts.

        Maps the column names from SQLAlchemy result rows to a consistent
        dict format.  The ``score`` column is rounded to 6 decimal places.

        Args:
            stmt: A SQLAlchemy ``select()`` statement with a ``score``
                label on the relevance column.

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
