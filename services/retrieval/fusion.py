"""Fusion — RRF merging, recency sorting, and rerank gating."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from middleware.metrics import reranker_latency_seconds
from packages.reranker import RRF_K


class FusionMixin:
    """RRF merge, recency sort, and optional rerank (composed into the retriever)."""

    _reranker: Any | None
    _rerank_top_n: int

    def _merge_and_sort(
        self,
        episode_vector_results: list[dict[str, Any]],
        fact_vector_results: list[dict[str, Any]],
        episode_bm25_results: list[dict[str, Any]],
        fact_bm25_results: list[dict[str, Any]],
        entity_results: list[dict[str, Any]],
        *,
        limit: int,
        rrf_top_n: int,
        sort: str,
    ) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
    ]:
        """RRF-merge per type, then apply the requested ordering.

        ``"relevance"`` keeps RRF order; ``"recent"`` re-sorts merged
        pages by ``created_at DESC`` server-side. Items carry ISO
        strings (BM25 legs), datetimes (vector legs), or nothing —
        normalized to epoch so mixed shapes never raise TypeError.

        Args:
            episode_vector_results: Episode vector leg results.
            fact_vector_results: Fact vector leg results.
            episode_bm25_results: Episode BM25 leg results.
            fact_bm25_results: Fact BM25 leg results.
            entity_results: Graph-BFS entity results (single source).
            limit: Max entities returned.
            rrf_top_n: RRF candidate pool size.
            sort: ``"relevance"`` or ``"recent"`` (already validated).

        Returns:
            Tuple of ``(episodes, facts, entities)`` result lists.
        """
        merged_episodes = self._rrf_merge(
            [episode_vector_results, episode_bm25_results],
            top_n=rrf_top_n,
        )
        merged_facts = self._rrf_merge(
            [fact_vector_results, fact_bm25_results],
            top_n=rrf_top_n,
        )
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
        return merged_episodes, merged_facts, entities

    async def _maybe_rerank(
        self,
        query: str,
        merged_episodes: list[dict[str, Any]],
        merged_facts: list[dict[str, Any]],
        sort: str,
        failed_legs: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Rerank merged pages when relevance sort and a reranker apply.

        Reranking is a relevance operation — skipped for
        ``sort="recent"`` (recency ordering must survive). A rerank
        failure degrades to unreranked RRF order instead of failing the
        search.

        Args:
            query: Natural-language search query.
            merged_episodes: RRF-merged episode results.
            merged_facts: RRF-merged fact results.
            sort: ``"relevance"`` or ``"recent"``.
            failed_legs: Mutable degraded-leg list (appended on failure).

        Returns:
            Tuple of ``(episodes, facts)``, reranked or untouched.
        """
        if self._reranker is None or sort != "relevance":
            return merged_episodes, merged_facts
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
            from services import hybrid_retriever as _hr  # via facade (patchable)
            _hr.logger.error(
                "hybrid_retriever.rerank_failed",
                extra={
                    "query": query[:100],
                    "duration_ms": round((time.monotonic() - _rerank_start) * 1000),
                    "leg": "reranker",
                    "error": str(exc),
                },
                exc_info=True,
            )
            # Rerank failure degrades to unreranked RRF order — the
            # retrieval legs already succeeded, so failing the whole
            # search here would discard good results.
            failed_legs.append("reranker")
        return merged_episodes, merged_facts

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
                raw = item.get("id")
                if not raw:
                    continue
                item_id = str(raw)

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
