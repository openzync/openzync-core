"""Hybrid retrieval — vector + BM25 + graph BFS with RRF fusion.

Combines three retrieval strategies and merges results using Reciprocal
Rank Fusion (RRF) for robust context retrieval:

1. **Vector search** (pgvector cosine similarity) — semantic matching.
2. **BM25 search** (PostgreSQL ``ts_rank``) — keyword / lexical matching.
3. **Graph BFS** — entity-relationship traversal via the configured
   ``GraphBackend`` (PostgreSQL recursive CTE, SurrealQL graph traversal,
   or FalkorDB Cypher).

The RRF formula is: ``score(d) = Σ 1 / (60 + rank_s(d))`` across
all three sources.  Results are deduplicated by source ID and the
top-N by merged score are returned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exceptions import SearchLegFailedError
from core.rls import release_tenant_context, set_tenant_context
from middleware.metrics import graph_search_latency_seconds, reranker_latency_seconds
from packages.reranker import DEFAULT_RERANK_TOP_K, DEFAULT_RERANK_TOP_N, RRF_K
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend
    from packages.reranker import CrossEncoderReranker
    from schemas.organization_config import OrgConfigBase

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

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
        sequentially on the shared session. If any single
        leg fails, the entire search fails with a
        ``SearchLegFailedError`` — no silent partial results are returned.

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

        Raises:
            SearchLegFailedError: If any retrieval leg (vector, BM25,
                graph BFS, or reranker) fails.
        """
        _search_start = time.monotonic()

        # Facts are filtered by the effective-at predicate against a time.
        # An explicit ``query_time`` (as-of) is threaded to the fact legs;
        # ``None`` (default/now queries) is left unset so each leg resolves
        # "now" at its own execution instant — plain queries must never
        # leak superseded facts (valid_to <= now).
        as_of_kwargs: dict[str, datetime] = (
            {} if query_time is None else {"query_time": query_time}
        )

        # ── Embed the query once; both vector legs reuse this vector ──────
        # Embedding failure propagates as SearchLegFailedError(leg="embedding")
        # before any leg-level try/except can re-wrap it.
        query_embedding = await self._embed_query(query, project_id)

        # ── Run all five retrieval legs ──────────────────────────────────
        # One AsyncSession allows a single in-flight operation, so each DB
        # leg runs on a sibling session from the app factory (same RLS
        # tenant, rolled back independently on failure). Without a factory
        # the legs run sequentially on the shared session — same results,
        # no parallelism. ``asyncio.gather`` on one shared session would
        # interleave operations on it, so the no-factory path awaits each
        # leg in turn instead of gathering.
        retrieval_limit = (
            max(limit, self._rerank_top_k) if self._reranker is not None else limit
        )

        if self._session_factory is None:
            episode_vector_results = await self._run_leg(
                "episode_vector",
                project_id,
                lambda r: r._vector_search_episodes(
                    query_embedding, project_id, retrieval_limit
                ),
            )
            fact_vector_results = await self._run_leg(
                "fact_vector",
                project_id,
                lambda r: r._vector_search_facts(
                    query_embedding, project_id, retrieval_limit, **as_of_kwargs
                ),
            )
            episode_bm25_results = await self._run_leg(
                "episode_bm25",
                project_id,
                lambda r: r._bm25_search_episodes(query, project_id, retrieval_limit),
            )
            fact_bm25_results = await self._run_leg(
                "fact_bm25",
                project_id,
                lambda r: r._bm25_search_facts(
                    query, project_id, retrieval_limit, **as_of_kwargs
                ),
            )
            entity_results = await self._run_leg(
                "graph_bfs",
                project_id,
                lambda r: r._graph_bfs_search(query, project_id, query_time=query_time),
            )
        else:
            (
                episode_vector_results,
                fact_vector_results,
                episode_bm25_results,
                fact_bm25_results,
                entity_results,
            ) = await asyncio.gather(
                self._run_leg(
                    "episode_vector",
                    project_id,
                    lambda r: r._vector_search_episodes(
                        query_embedding, project_id, retrieval_limit
                    ),
                ),
                self._run_leg(
                    "fact_vector",
                    project_id,
                    lambda r: r._vector_search_facts(
                        query_embedding, project_id, retrieval_limit, **as_of_kwargs
                    ),
                ),
                self._run_leg(
                    "episode_bm25",
                    project_id,
                    lambda r: r._bm25_search_episodes(query, project_id, retrieval_limit),
                ),
                self._run_leg(
                    "fact_bm25",
                    project_id,
                    lambda r: r._bm25_search_facts(
                        query, project_id, retrieval_limit, **as_of_kwargs
                    ),
                ),
                self._run_leg(
                    "graph_bfs",
                    project_id,
                    lambda r: r._graph_bfs_search(query, project_id, query_time=query_time),
                ),
            )

        # ── RRF merge per type ────────────────────────────────────────────
        # Use rerank_top_k as the RRF candidate pool size when re-ranking is active
        rrf_top_n = self._rerank_top_k if self._reranker is not None else limit

        if sort not in ("relevance", "recent"):
            from core.exceptions import ValidationError

            raise ValidationError(f"Invalid sort: {sort!r}")

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

        if sort == "recent":
            # note: recent re-sorts merged pages by created_at DESC
            # server-side. Items carry ISO strings (BM25 legs), datetimes
            # (vector legs), or nothing — normalized to epoch so mixed
            # shapes never raise TypeError in the comparison.
            merged_episodes = sorted(
                merged_episodes,
                key=lambda d: (
                    self._to_epoch(d.get("created_at")),
                    str(d.get("id") or ""),
                ),
                reverse=True,
            )
            merged_facts = sorted(
                merged_facts,
                key=lambda d: (
                    self._to_epoch(d.get("created_at")),
                    str(d.get("id") or ""),
                ),
                reverse=True,
            )
            entities = sorted(
                entities,
                key=lambda d: (
                    self._to_epoch(d.get("created_at")),
                    str(d.get("id") or ""),
                ),
                reverse=True,
            )

        # ── Re-ranking step ───────────────────────────────────────────────
        # note: reranking is a relevance operation — skipped for
        # sort=recent (recency ordering must survive).
        if self._reranker is not None and sort == "relevance":
            _rerank_start = time.monotonic()
            try:
                merged_episodes = await self._reranker.rerank(
                    query,
                    merged_episodes,
                    top_n=self._rerank_top_n,
                )
                merged_facts = await self._reranker.rerank(
                    query,
                    merged_facts,
                    top_n=self._rerank_top_n,
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
                raise SearchLegFailedError(
                    leg_name="reranker", original_error=str(exc)
                ) from exc

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
        }

    # ── Leg plumbing ─────────────────────────────────────────────────────────

    def _scoped(self, db: AsyncSession) -> HybridRetriever:
        """Return a retriever with identical config bound to another session.

        Used to run one retrieval leg on a sibling session — a single
        ``AsyncSession`` allows only one in-flight operation, so
        concurrent legs cannot share ``self._db``.

        Args:
            db: The sibling session the leg runs on.

        Returns:
            A new :class:`HybridRetriever` sharing org, backends, reranker
            settings, org config, reranker, and Redis, but no session
            factory (no nesting — the parent fans out, legs never re-fan).
        """
        return HybridRetriever(
            db,
            self._org_id,
            redis=self._redis,
            graph_backends=self._graph_backends,
            org_config=self._org_config,
            reranker=self._reranker,
            session_factory=None,
        )

    async def _run_leg(
        self,
        leg_name: str,
        project_id: UUID,
        run: Callable[[HybridRetriever], Awaitable[list[dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        """Run one retrieval leg, failing loud as ``SearchLegFailedError``.

        With a session factory the leg runs on a sibling session bound to
        this org's RLS tenant; a leg failure rolls back only that session.
        Without a factory the leg runs sequentially on the shared session.
        ``SearchLegFailedError`` from the leg itself passes through
        unwrapped; any other exception is logged with leg context and
        wrapped. ``CancelledError`` is never swallowed.

        Args:
            leg_name: Leg identifier for logs and the raised error.
            project_id: Scoped project UUID (log context).
            run: Takes the retriever bound to the leg's session and returns
                that leg's result list.

        Returns:
            The leg's result list.

        Raises:
            SearchLegFailedError: If the leg fails.
        """
        extra = {
            "leg": leg_name,
            "org_id": str(self._org_id),
            "project_id": str(project_id),
        }
        if self._session_factory is None:
            logger.warning("hybrid_retriever.sequential_leg", extra=extra)
            try:
                return await run(self)
            except SearchLegFailedError:
                raise
            except Exception as exc:
                logger.error(
                    f"hybrid_retriever.{leg_name}_failed", extra=extra, exc_info=True
                )
                await self._db.rollback()
                raise SearchLegFailedError(
                    leg_name=leg_name, original_error=str(exc)
                ) from exc
        async with self._session_factory() as leg_db:
            await set_tenant_context(leg_db, self._org_id)
            try:
                return await run(self._scoped(leg_db))
            except SearchLegFailedError:
                await leg_db.rollback()
                raise
            except Exception as exc:
                logger.error(
                    f"hybrid_retriever.{leg_name}_failed", extra=extra, exc_info=True
                )
                await leg_db.rollback()
                raise SearchLegFailedError(
                    leg_name=leg_name, original_error=str(exc)
                ) from exc
            finally:
                await release_tenant_context(leg_db)

    @staticmethod
    def _to_epoch(value: Any) -> float:
        """Normalize a ``created_at`` shape to epoch seconds for sorting.

        Legs disagree on shape: vector legs return datetimes, BM25 legs
        return ISO strings, graph backends may return either or nothing.
        Missing/unparseable values sort as ``0.0`` (oldest) instead of
        raising ``TypeError`` on mixed-type comparison.

        Args:
            value: A datetime, ISO-8601 string, epoch number, or ``None``.

        Returns:
            Epoch seconds, or ``0.0`` when absent or unparseable.
        """
        if value is None:
            return 0.0
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return 0.0
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        return 0.0

    # ── Vector Search ──────────────────────────────────────────────────────────

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
        """Semantic search over episodes using pgvector cosine similarity.

        Finds the nearest neighbours in ``episodes.embedding`` using the
        ``<=>`` (cosine distance) operator, against the precomputed query
        embedding.  The embedding is generated once upstream in
        ``hybrid_search`` and shared across both vector legs.

        The ``embedding`` column is ``VECTOR(768)`` — the frozen
        canonical dimension (``core.embeddings.CANONICAL_EMBED_DIM``).
        The cast is a no-op type assertion that keeps the SQLAlchemy
        ``<=>`` operator typed; the query vector is validated to exactly
        768 dims upstream in ``_embed_query``.

        Args:
            query_embedding: Precomputed embedding vector for the query.
            project_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``role``,
            ``score``, and ``created_at`` keys.
        """
        from core.embeddings import CANONICAL_EMBED_DIM

        # Raw ``text()`` — the pgvector asyncpg codec (registered via
        # ``init_db_engine``) encodes the native ``list[float]`` directly;
        # binding through ``Vector``'s bind_processor stringified it and the
        # codec re-encoded the string (asyncpg DataError → 503). Mirrors
        # ``workers/tasks/embed_episode.py``.
        stmt = text(
            "SELECT id, content, role, created_at, "  # noqa: S608
            "1.0 - coalesce(embedding <=> "
            f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})), 1.0) AS score "
            "FROM episodes "
            "WHERE project_id = :project_id "
            "AND is_deleted = false "
            "AND embedding IS NOT NULL "
            "AND vector_dims(embedding) > 0 "
            # Raw operator in ORDER BY (not the SELECT alias): only this
            # form matches the HNSW index.  `ORDER BY score` sorts on a
            # computed expression and forces a seq scan + full sort.
            "ORDER BY embedding <=> "
            f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})) "
            "LIMIT :limit"
            # S608 justification: interpolates the int constant
            # CANONICAL_EMBED_DIM into a static CAST, never user input.
        ).bindparams(
            embedding=query_embedding,
            project_id=project_id,
            limit=limit,
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
        """Semantic search over facts using pgvector cosine similarity.

        Same pattern as ``_vector_search_episodes`` but operates on the
        ``facts.embedding`` column, against the precomputed query embedding
        shared from ``hybrid_search``.  Facts are filtered by the
        effective-at predicate against ``query_time`` (``None`` → now) so
        superseded (``valid_to`` set) and not-yet-valid (``valid_from``
        future) rows never leak into results.

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
        effective_time = query_time or datetime.now(UTC)

        from core.embeddings import CANONICAL_EMBED_DIM

        # Raw ``text()`` — same codec contract as ``_vector_search_episodes``.
        stmt = text(
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
            # Raw operator in ORDER BY (not the SELECT alias): only this
            # form matches the HNSW index — see _vector_search_episodes.
            "ORDER BY embedding <=> "
            f"CAST(:embedding AS vector({CANONICAL_EMBED_DIM})) "
            "LIMIT :limit"
            # S608 justification: interpolates the int constant
            # CANONICAL_EMBED_DIM into a static CAST, never user input.
        ).bindparams(
            embedding=query_embedding,
            project_id=project_id,
            effective_time=effective_time,
            limit=limit,
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

        Delegates to :meth:`EpisodeRepository.search_by_bm25` — this service
        keeps merge/sort only, no SQL. Uses ``plainto_tsquery`` for
        user-friendly query parsing and ``ts_rank`` for BM25-like
        relevance scoring.

        Args:
            query: Keyword query string.
            project_id: Scoped user UUID.
            limit: Max results.

        Returns:
            A list of result dicts with ``id``, ``content``, ``role``,
            ``score``, and ``created_at`` keys.
        """
        return await EpisodeRepository(self._db).search_by_bm25(
            query=query,
            project_id=project_id,
            org_id=self._org_id,
            limit=limit,
        )

    async def _bm25_search_facts(
        self,
        query: str,
        project_id: UUID,
        limit: int = 20,
        query_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Keyword search over facts using PostgreSQL full-text search.

        Delegates to :meth:`FactRepository.search_by_bm25` — this service
        keeps merge/sort only, no SQL. Facts are filtered by the
        effective-at predicate against ``query_time`` (``None`` → now) —
        same supersession semantics as the vector fact leg.

        Args:
            query: Keyword query string.
            project_id: Scoped user UUID.
            limit: Max results.
            query_time: Effective-at instant for the validity predicate.

        Returns:
            A list of result dicts with ``id``, ``content``, ``subject``,
            ``predicate``, ``object``, ``score``, ``confidence``,
            ``valid_from``, ``valid_to``, and ``invalid_at`` keys.
        """
        return await FactRepository(self._db).search_by_bm25(
            query=query,
            project_id=project_id,
            org_id=self._org_id,
            limit=limit,
            effective_at=query_time,
        )

    # ── Graph BFS Search ───────────────────────────────────────────────────────

    async def _graph_bfs_search(
        self,
        query: str,
        project_id: UUID,
        query_time: datetime | None = None,
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
            query_time: Effective-at instant for edge traversal.  Passed
                as ``as_of`` to each backend's ``retrieve_graph`` — edges
                whose validity range does not contain this instant are
                not traversed.  ``None`` resolves to now in the backend.

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

        # The graph BFS leg is now as-of-accurate: the same ``query_time``
        # the fact legs filter on is threaded through as ``as_of`` so a
        # historical query sees the graph exactly as it was at that
        # instant, not the current topology.
        results = await asyncio.gather(
            *[
                backend.retrieve_graph(
                    org_id=self._org_id,
                    project_id=project_id,
                    query=query,
                    as_of=query_time,
                )
                for backend in self._graph_backends
            ],
        )

        # Merge: deduplicate by id, keep first occurrence (lower distance wins)
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for result_list in results:
            for item in result_list:
                if not isinstance(item, dict):
                    logger.warning(
                        "hybrid_retriever.graph_item_malformed",
                        extra={"org_id": str(self._org_id)},
                    )
                    continue
                item_id = item.get("id")
                if not item_id:
                    logger.warning(
                        "hybrid_retriever.graph_item_missing_id",
                        extra={"org_id": str(self._org_id)},
                    )
                    continue
                item_id = str(item_id)
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    merged.append(item)

        # Sort by distance (closest first), limit to MAX_BFS_RESULTS.
        # A missing/non-numeric distance sorts last instead of raising.
        def _distance(item: dict[str, Any]) -> float:
            distance = item.get("distance")
            if isinstance(distance, bool) or not isinstance(distance, (int, float)):
                return float("inf")
            return float(distance)

        merged.sort(key=_distance)
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
