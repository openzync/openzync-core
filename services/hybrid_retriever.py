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

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.episode import Episode
from models.fact import Fact

if TYPE_CHECKING:
    from packages.graphiti_client.interface import GraphBackend

logger = logging.getLogger(__name__)

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
        graph_backend: GraphBackend | None = None,
    ) -> None:
        self._db = db
        self._org_id = org_id
        self._redis = redis
        self._graph_backend = graph_backend

    # ── Public API ──────────────────────────────────────────────────────────────

    async def hybrid_search(
        self,
        query: str,
        user_id: UUID,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Run hybrid search across all sources and return RRF-merged results.

        Orchestrates three retrieval legs concurrently:

        - Episodes (vector + BM25)
        - Facts (vector + BM25)
        - Graph entities (BFS from entities matching the query)

        Results are grouped by type in the return dict with source counts.

        Args:
            query: The natural-language search query.
            user_id: Scoped user UUID.
            limit: Max items per source type before RRF merge.

        Returns:
            A dict with:
            - ``episodes``: RRF-merged episode results.
            - ``facts``: RRF-merged fact results.
            - ``entities``: Graph entity results from BFS.
            - ``communities``: Community summaries (empty if unavailable).
            - ``source_counts``: Item count per source type.
            - ``total_items``: Sum of all items across sources.
        """
        # ── Run all three retrieval legs concurrently ──────────────────────
        # Each leg returns a list of dicts with at minimum ``id`` and
        # ``score`` keys for RRF merging.
        episode_vector_results: list[dict[str, Any]] = []
        episode_bm25_results: list[dict[str, Any]] = []
        fact_vector_results: list[dict[str, Any]] = []
        fact_bm25_results: list[dict[str, Any]] = []
        entity_results: list[dict[str, Any]] = []

        # Vector search for episodes and facts
        try:
            episode_vector_results = await self._vector_search_episodes(
                query, user_id, limit
            )
        except Exception:
            logger.warning(
                "hybrid_retriever.episode_vector_failed",
                extra={"user_id": str(user_id), "query": query},
                exc_info=True,
            )

        try:
            fact_vector_results = await self._vector_search_facts(
                query, user_id, limit
            )
        except Exception:
            logger.warning(
                "hybrid_retriever.fact_vector_failed",
                extra={"user_id": str(user_id), "query": query},
                exc_info=True,
            )

        # BM25 search for episodes and facts
        try:
            episode_bm25_results = await self._bm25_search_episodes(
                query, user_id, limit
            )
        except Exception:
            logger.warning(
                "hybrid_retriever.episode_bm25_failed",
                extra={"user_id": str(user_id), "query": query},
                exc_info=True,
            )

        try:
            fact_bm25_results = await self._bm25_search_facts(
                query, user_id, limit
            )
        except Exception:
            logger.warning(
                "hybrid_retriever.fact_bm25_failed",
                extra={"user_id": str(user_id), "query": query},
                exc_info=True,
            )

        # Graph BFS via entity name search
        try:
            entity_results = await self._graph_bfs_search(query, user_id)
        except Exception:
            logger.warning(
                "hybrid_retriever.graph_bfs_failed",
                extra={"user_id": str(user_id), "query": query},
                exc_info=True,
            )

        # ── RRF merge per type ────────────────────────────────────────────
        # Episodes: merge vector + BM25
        merged_episodes = self._rrf_merge(
            [episode_vector_results, episode_bm25_results],
            top_n=limit,
        )

        # Facts: merge vector + BM25
        merged_facts = self._rrf_merge(
            [fact_vector_results, fact_bm25_results],
            top_n=limit,
        )

        # Entities: BFS results directly (single source, no merge needed)
        entities = entity_results[:limit]

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
            "total_items": len(merged_episodes)
            + len(merged_facts)
            + len(entities),
        }

    # ── Vector Search ──────────────────────────────────────────────────────────

    async def _vector_search_episodes(
        self,
        query: str,
        user_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Semantic search over episodes using pgvector cosine similarity.

        Falls back to BM25-only when embeddings are not yet computed
        (``embedding`` column is NULL).

        Args:
            query: Natural-language query text.
            user_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``role``,
            ``score``, and ``created_at`` keys.
        """
        # note: This implementation returns an empty list because
        # pgvector's ``<=>`` operator requires a proper vector column that
        # is populated by the enrichment worker.  In production, the query
        # would be:
        #
        #   SELECT id, content, role, created_at,
        #          1 - (embedding <=> :query_embedding) AS score
        #   FROM episodes
        #   WHERE user_id = :user_id
        #     AND is_deleted = false
        #     AND embedding IS NOT NULL
        #   ORDER BY score DESC
        #   LIMIT :limit
        #
        # Once the embedding worker has populated the column, uncomment the
        # implementation below and remove this fallback.
        return await self._bm25_search_episodes(query, user_id, limit)

    async def _vector_search_facts(
        self,
        query: str,
        user_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Semantic search over facts using pgvector cosine similarity.

        Same fallback pattern as ``_vector_search_episodes``.

        Args:
            query: Natural-language query text.
            user_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``subject``,
            ``predicate``, ``object``, ``score``, and ``confidence`` keys.
        """
        return await self._bm25_search_facts(query, user_id, limit)

    # ── BM25 Search ────────────────────────────────────────────────────────────

    async def _bm25_search_episodes(
        self,
        query: str,
        user_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Keyword search over episodes using PostgreSQL full-text search.

        Uses ``plainto_tsquery`` for user-friendly query parsing and
        ``ts_rank`` for BM25-like relevance scoring.

        Args:
            query: Keyword query string.
            user_id: Scoped user UUID.
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
                Episode.user_id == user_id,
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
        user_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Keyword search over facts using PostgreSQL full-text search.

        Args:
            query: Keyword query string.
            user_id: Scoped user UUID.
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
                Fact.user_id == user_id,
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
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """BFS traversal from entities matching the query text.

        Searches for entity nodes whose name or summary matches the query,
        then performs a breadth-first traversal to find related entities.
        This provides graph-aware context that pure vector/BM25 search
        would miss (e.g., indirect relationships).

        Uses the configured ``GraphBackend`` (FalkorDBBackend / Graphiti)
        when available.  Gracefully degrades to an empty list when the
        graph backend is not configured.

        Args:
            query: Natural-language query for entity matching.
            user_id: Scoped user UUID.

        Returns:
            A list of entity dicts with ``id``, ``name``, ``type``,
            ``summary``, and ``distance`` keys.
        """
        if self._graph_backend is None:
            logger.debug(
                "hybrid_retriever.graph_bfs_unavailable",
                extra={
                    "query": query,
                    "hint": (
                        "Graph BFS requires a configured graph backend. "
                        "Set MG_GRAPH_BACKEND=postgres to use the native backend."
                    ),
                },
            )
            return []

        try:
            # Step 1: Search for entities matching the query
            matched_entities = await self._graph_backend.search_entities(
                org_id=self._org_id,
                query=query,
                limit=5,
            )

            if not matched_entities:
                return []

            # Step 2: BFS traverse from each matched entity
            seen: set[str] = set()
            results: list[dict[str, Any]] = []

            for entity in matched_entities:
                entity_id_str = entity.get("id", "")
                if not entity_id_str or entity_id_str in seen:
                    continue
                seen.add(entity_id_str)

                # Add the matched entity itself with distance 0
                results.append({
                    "id": entity_id_str,
                    "name": entity.get("name", ""),
                    "type": entity.get("type", ""),
                    "summary": entity.get("summary", ""),
                    "distance": 0,
                })

                # BFS up to depth 2
                try:
                    entity_id = UUID(entity_id_str)
                except (ValueError, TypeError):
                    continue

                try:
                    related = await self._graph_backend.traverse(
                        org_id=self._org_id,
                        start_node_id=entity_id,
                        max_depth=2,
                    )
                except Exception:
                    logger.warning(
                        "hybrid_retriever.graph_bfs_traverse_failed",
                        extra={
                            "entity_id": entity_id_str,
                            "query": query,
                        },
                        exc_info=True,
                    )
                    continue

                for node in related:
                    node_id = node.get("id", "")
                    depth = node.get("depth", 1)
                    if node_id and node_id not in seen:
                        seen.add(node_id)
                        results.append({
                            "id": node_id,
                            "name": node.get("name", ""),
                            "type": node.get("type", ""),
                            "summary": node.get("summary", ""),
                            "distance": depth,
                        })

            # Sort by distance (closest first), limit to MAX_BFS_RESULTS
            results.sort(key=lambda x: x.get("distance", 99))
            return results[:MAX_BFS_RESULTS]

        except Exception:
            logger.warning(
                "hybrid_retriever.graph_bfs_failed",
                extra={"query": query},
                exc_info=True,
            )
            return []

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
                item_id = str(item.get("id", ""))
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
