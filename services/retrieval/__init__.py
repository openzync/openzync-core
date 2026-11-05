"""Hybrid retriever — facade composing vector, lexical, and fusion mixins.

Sub-package layout (one responsibility per module)::

    vector_search.py        query embedding + pgvector legs
    lexical_graph_search.py BM25 legs + graph-BFS traversal
    legs.py                 sibling-session plumbing + leg entry points
    fusion.py               RRF merge, recency sort, rerank gating

SQL lives in :class:`repositories.retrieval_repository.RetrievalRepository`
— this package passes ``(embedding, project_id, limit)`` only.
``HybridRetriever`` keeps the original import path and public API
(``hybrid_search``); routers are unchanged.
``services/hybrid_retriever.py`` re-exports it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exceptions import SearchLegFailedError
from middleware.metrics import graph_search_latency_seconds
from packages.reranker import DEFAULT_RERANK_TOP_K, DEFAULT_RERANK_TOP_N
from services.retrieval.fusion import FusionMixin
from services.retrieval.legs import LegDispatchMixin
from services.retrieval.lexical_graph_search import LexicalGraphSearchMixin
from services.retrieval.vector_search import VectorSearchMixin

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend
    from packages.reranker import CrossEncoderReranker
    from schemas.organization_config import OrgConfigBase

logger = logging.getLogger(__name__)


class HybridRetriever(
    VectorSearchMixin,
    LexicalGraphSearchMixin,
    LegDispatchMixin,
    FusionMixin,
):
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
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._db = db
        self._org_id = org_id
        self._redis = redis
        self._graph_backends = graph_backends or []
        self._org_config = org_config
        self._reranker = reranker
        self._session_factory = session_factory
        """Sibling-session factory for concurrent legs (``None`` = sequential)."""
        self._last_query_embedding_dim: int | None = None
        """Dimension of the most recently generated query embedding, or ``None`` if
        no embedding has been generated yet (e.g. all vector legs failed).  Used
        by callers for diagnostic logging to detect dimension mismatches."""
        self._rerank_top_k: int = (
            org_config.reranker_top_k
            if org_config and org_config.reranker_top_k
            else DEFAULT_RERANK_TOP_K
        )
        self._rerank_top_n: int = (
            org_config.reranker_top_n
            if org_config and org_config.reranker_top_n
            else DEFAULT_RERANK_TOP_N
        )

    # ── Public API ──────────────────────────────────────────────────────────────

    async def hybrid_search(
        self,
        query: str,
        project_id: UUID,
        limit: int = 20,
        query_time: datetime | None = None,
        sort: str = "relevance",
    ) -> dict[str, Any]:
        """Run hybrid search across all sources and return RRF-merged results.

        Orchestrates five retrieval legs:

        - Episodes (vector + BM25)
        - Facts (vector + BM25)
        - Graph entities (BFS from entities matching the query)

        The query is embedded exactly once and the resulting vector is
        shared by both vector legs (episodes and facts). Legs run
        concurrently via ``asyncio.gather`` when a sibling-session
        factory is available — one AsyncSession allows a single
        in-flight operation, so without a factory the legs run
        sequentially on the shared session. A single leg failing no
        longer fails the search: the leg contributes no results, the
        failure is logged with leg context, and the response carries
        ``degraded=True`` plus the ``failed_legs`` names. Only when all
        five legs fail does the search raise ``SearchLegFailedError``.

        Results are grouped by type in the return dict with source counts.

        Args:
            query: The natural-language search query.
            project_id: Scoped user UUID.
            limit: Max items per source type before RRF merge.
            query_time: Effective-at timestamp for retrieval (UTC).
                Facts whose validity range contains this instant are
                returned; ``None`` means "now" (resolved at call time).
                Episodes are not temporally validatable and ignore this.
                The graph-BFS leg traverses edges effective at this
                instant (as-of).
            sort: ``"relevance"`` (default RRF ranking) or ``"recent"``
                (``created_at DESC`` per source type, applied server-side
                after merging).

        Returns:
            A dict with:
            - ``episodes``: RRF-merged episode results.
            - ``facts``: RRF-merged fact results.
            - ``entities``: Graph entity results from BFS.
            - ``communities``: Community summaries (empty if unavailable).
            - ``source_counts``: Item count per source type.
            - ``total_items``: Sum of all items across sources.
            - ``degraded``: ``True`` when one or more legs failed and the
              results are partial (RRF fusion ran over the available legs).
            - ``failed_legs``: Names of the legs that failed (empty when none).

        Raises:
            SearchLegFailedError: Only when *all* retrieval legs fail.
                Single-leg failures degrade to partial results instead.
        """
        _search_start = time.monotonic()

        # Facts are filtered by the effective-at predicate against a time.
        # An explicit ``query_time`` (as-of) is threaded to the fact legs;
        # ``None`` (default/now queries) is left unset so each leg resolves
        # "now" at its own execution instant — plain queries must never
        # leak superseded facts (valid_to <= now).

        # ── Embed the query once; both vector legs share this vector ──────
        # An embedding failure degrades (not raises): both vector legs
        # contribute nothing while BM25/graph legs still run. Only when
        # every leg fails does hybrid_search raise (checked below).
        failed_legs: list[str] = []
        retrieval_limit = (
            max(limit, self._rerank_top_k) if self._reranker is not None else limit
        )
        try:
            query_embedding = await self._embed_query(query, project_id)
        except asyncio.CancelledError:
            raise
        except SearchLegFailedError as exc:
            logger.warning(
                "hybrid_retriever.embedding_degraded",
                extra={
                    "org_id": str(self._org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            query_embedding = None
            failed_legs.extend(["episode_vector", "fact_vector"])

        async def _safe_leg(
            leg_name: str, coro: Awaitable[list[dict[str, Any]]]
        ) -> list[dict[str, Any]]:
            """Await one leg, degrading to ``[]`` on leg failure.

            ``CancelledError`` is never swallowed — it propagates so task
            cancellation still cancels the search.
            """
            try:
                return await coro
            except asyncio.CancelledError:
                raise
            except SearchLegFailedError as exc:
                failed_legs.append(leg_name)
                logger.warning(
                    "hybrid_retriever.leg_degraded",
                    extra={
                        "leg": leg_name,
                        "org_id": str(self._org_id),
                        "project_id": str(project_id),
                        "error": str(exc),
                    },
                )
                return []

        if self._session_factory is None:
            episode_vector_results = await _safe_leg(
                "episode_vector",
                self._leg_episode_vector(query_embedding, project_id, retrieval_limit),
            )
            fact_vector_results = await _safe_leg(
                "fact_vector",
                self._leg_fact_vector(
                    query_embedding, project_id, retrieval_limit, query_time
                ),
            )
            episode_bm25_results = await _safe_leg(
                "episode_bm25",
                self._leg_episode_bm25(query, project_id, retrieval_limit),
            )
            fact_bm25_results = await _safe_leg(
                "fact_bm25",
                self._leg_fact_bm25(query, project_id, retrieval_limit, query_time),
            )
            entity_results = await _safe_leg(
                "graph_bfs", self._leg_graph_bfs(query, project_id, query_time)
            )
        else:
            (
                episode_vector_results,
                fact_vector_results,
                episode_bm25_results,
                fact_bm25_results,
                entity_results,
            ) = await asyncio.gather(
                _safe_leg(
                    "episode_vector",
                    self._leg_episode_vector(
                        query_embedding, project_id, retrieval_limit
                    ),
                ),
                _safe_leg(
                    "fact_vector",
                    self._leg_fact_vector(
                        query_embedding, project_id, retrieval_limit, query_time
                    ),
                ),
                _safe_leg(
                    "episode_bm25",
                    self._leg_episode_bm25(query, project_id, retrieval_limit),
                ),
                _safe_leg(
                    "fact_bm25",
                    self._leg_fact_bm25(query, project_id, retrieval_limit, query_time),
                ),
                _safe_leg(
                    "graph_bfs", self._leg_graph_bfs(query, project_id, query_time)
                ),
            )

        if all(
            leg in failed_legs
            for leg in (
                "episode_vector",
                "fact_vector",
                "episode_bm25",
                "fact_bm25",
                "graph_bfs",
            )
        ):
            raise SearchLegFailedError(
                leg_name="all",
                original_error=f"All retrieval legs failed: {', '.join(failed_legs)}.",
            )

        # ── RRF merge per type ────────────────────────────────────────────
        # Use rerank_top_k as the RRF candidate pool size when re-ranking is active
        rrf_top_n = self._rerank_top_k if self._reranker is not None else limit

        if sort not in ("relevance", "recent"):
            from core.exceptions import ValidationError

            raise ValidationError(f"Invalid sort: {sort!r}")

        merged_episodes, merged_facts, entities = self._merge_and_sort(
            episode_vector_results,
            fact_vector_results,
            episode_bm25_results,
            fact_bm25_results,
            entity_results,
            limit=limit,
            rrf_top_n=rrf_top_n,
            sort=sort,
        )

        # ── Re-ranking step (relevance only) ──────────────────────────────
        merged_episodes, merged_facts = await self._maybe_rerank(
            query, merged_episodes, merged_facts, sort, failed_legs
        )

        graph_search_latency_seconds.labels(org_id=str(self._org_id)).observe(
            time.monotonic() - _search_start
        )

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
            "query_embedding_dim": self._last_query_embedding_dim,
            "degraded": bool(failed_legs),
            "failed_legs": failed_legs,
        }


__all__ = ["HybridRetriever"]
