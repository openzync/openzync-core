"""Lexical + graph search — BM25 legs and graph-BFS traversal."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MAX_BFS_RESULTS: int = 50
"""Max results from the graph BFS leg before RRF merging."""


class LexicalGraphSearchMixin:
    """BM25 keyword legs plus graph-BFS traversal (composed into the retriever)."""

    _db: AsyncSession
    _org_id: UUID
    _graph_backends: list[Any]

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
        from services import hybrid_retriever as _hr  # via facade (patchable)
        return await _hr.EpisodeRepository(self._db).search_by_bm25(
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
        from services import hybrid_retriever as _hr  # via facade (patchable)
        return await _hr.FactRepository(self._db).search_by_bm25(
            query=query,
            project_id=project_id,
            org_id=self._org_id,
            limit=limit,
            effective_at=query_time,
        )
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
            by distance ascending.  A failing backend contributes no
            results — it never fails the whole BFS leg.
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
        # instant, not the current topology.  Each backend is guarded
        # individually — one backend down degrades that leg instead of
        # killing the whole BFS.
        async def _safe_retrieve(backend: Any) -> list[dict[str, Any]]:
            try:
                result: list[dict[str, Any]] = await backend.retrieve_graph(
                    org_id=self._org_id,
                    project_id=project_id,
                    query=query,
                    as_of=query_time,
                )
            except Exception:
                logger.warning(
                    "hybrid_retriever.graph_backend_failed",
                    extra={
                        "org_id": str(self._org_id),
                        "backend": type(backend).__name__,
                    },
                    exc_info=True,
                )
                return []
            return result if isinstance(result, list) else []

        results = await asyncio.gather(
            *[_safe_retrieve(backend) for backend in self._graph_backends],
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
                if item_id not in seen:
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
