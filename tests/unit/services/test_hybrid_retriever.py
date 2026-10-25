"""Unit tests for HybridRetriever — RRF fusion and hybrid search orchestration.

Tests the static ``_rrf_merge`` method directly (pure algorithm, no I/O) and
``hybrid_search`` with all retrieval legs mocked at the service boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.exceptions import SearchLegFailedError
from services.hybrid_retriever import MAX_BFS_RESULTS, HybridRetriever


@pytest.mark.unit
class TestHybridRetriever:
    """Unit tests for ``HybridRetriever`` — RRF fusion and search orchestration."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _make_item(item_id: str, score: float, **extra: Any) -> dict[str, Any]:
        """Build a result dict matching what retrieval legs return."""
        return {"id": item_id, "score": score, **extra}

    def _make_service(self) -> tuple[HybridRetriever, AsyncMock]:
        """Create a HybridRetriever with mocked DB session and single-embed leg.

        The merged retriever embeds the query exactly once in
        ``hybrid_search`` and shares the vector across both vector legs, so
        orchestration tests stub ``_embed_query`` instead of hitting the
        real LLM backend.
        """
        mock_db = AsyncMock()
        service = HybridRetriever(db=mock_db, org_id=self.ORG_ID)
        service._embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
        return service, mock_db

    # ── _rrf_merge — static method, pure logic, no mocking needed ────────────

    def test_rrf_merge_combines_two_lists(self) -> None:
        """RRF merge combines items from both ranked lists by fused score."""
        list_a = [
            self._make_item("id1", 0.9),
            self._make_item("id2", 0.8),
            self._make_item("id3", 0.7),
        ]
        list_b = [
            self._make_item("id2", 0.85),
            self._make_item("id4", 0.75),
            self._make_item("id5", 0.65),
        ]

        merged = HybridRetriever._rrf_merge([list_a, list_b], top_n=5)

        # All 5 unique items should appear
        assert len(merged) == 5
        merged_ids = {item["id"] for item in merged}
        assert merged_ids == {"id1", "id2", "id3", "id4", "id5"}
        # id2 appears in both lists → highest RRF score → ranked first
        assert merged[0]["id"] == "id2"
        assert "rrf_score" in merged[0]

    def test_rrf_merge_single_list_passthrough(self) -> None:
        """RRF merge of a single list returns items in original order."""
        items = [
            self._make_item("a", 0.9),
            self._make_item("b", 0.8),
        ]
        merged = HybridRetriever._rrf_merge([items], top_n=5)
        assert len(merged) == 2
        assert merged[0]["id"] == "a"
        assert merged[1]["id"] == "b"

    def test_rrf_merge_dedup_same_id_multiple_lists(self) -> None:
        """Same result ID from multiple lists is deduplicated — one entry with fused score."""
        list_a = [self._make_item("dup", 0.9)]
        list_b = [self._make_item("dup", 0.85), self._make_item("other", 0.7)]

        merged = HybridRetriever._rrf_merge([list_a, list_b], top_n=5)

        assert len(merged) == 2
        dup = [m for m in merged if m["id"] == "dup"]
        assert len(dup) == 1
        # rrf_score should reflect contributions from both lists
        # RRF_K=60, rank 1 in both lists → 1/61 + 1/61, rounded to 6dp
        expected = round(1.0 / 61 + 1.0 / 61, 6)
        assert dup[0]["rrf_score"] == expected

    def test_rrf_merge_empty_lists(self) -> None:
        """RRF merge of empty lists returns empty list."""
        merged = HybridRetriever._rrf_merge([[], []], top_n=5)
        assert merged == []

    def test_rrf_merge_respects_top_n(self) -> None:
        """RRF merge caps results at top_n."""
        list_a = [self._make_item(f"id{i}", 0.9) for i in range(10)]
        list_b = [self._make_item(f"id{i}", 0.8) for i in range(10)]

        merged = HybridRetriever._rrf_merge([list_a, list_b], top_n=3)
        assert len(merged) == 3

    # ── _rrf_merge — edge cases ─────────────────────────────────────────────

    def test_rrf_merge_empty_sublist(self) -> None:
        """RRF merge skips empty sub-lists gracefully."""
        items = [self._make_item("a", 0.9)]
        merged = HybridRetriever._rrf_merge([[], items, []], top_n=5)
        assert len(merged) == 1
        assert merged[0]["id"] == "a"

    # ── hybrid_search — orchestration with mocked legs ──────────────────────

    @pytest.mark.asyncio
    async def test_hybrid_search_success(self) -> None:
        """Happy path: all five legs return results, merged by RRF."""
        service, _mock_db = self._make_service()
        query = "test query"
        limit = 20

        # Mock all five retrieval legs to return controlled results
        mock_vector_eps = [self._make_item("v1", 0.9, content="v1")]
        mock_vector_facts = [self._make_item("vf1", 0.85, content="vf1")]
        mock_bm25_eps = [self._make_item("b1", 0.8, content="b1")]
        mock_bm25_facts = [self._make_item("bf1", 0.75, content="bf1")]
        mock_entity = [self._make_item("e1", 1.0, distance=0.5)]

        service._vector_search_episodes = AsyncMock(return_value=mock_vector_eps)
        service._vector_search_facts = AsyncMock(return_value=mock_vector_facts)
        service._bm25_search_episodes = AsyncMock(return_value=mock_bm25_eps)
        service._bm25_search_facts = AsyncMock(return_value=mock_bm25_facts)
        service._graph_bfs_search = AsyncMock(return_value=mock_entity)

        result = await service.hybrid_search(query, self.PROJECT_ID, limit=limit)

        assert "episodes" in result
        assert "facts" in result
        assert "entities" in result
        assert "source_counts" in result
        assert result["total_items"] >= 0
        # All five leg mocks were called
        service._vector_search_episodes.assert_awaited_once()
        service._vector_search_facts.assert_awaited_once()
        service._bm25_search_episodes.assert_awaited_once()
        service._bm25_search_facts.assert_awaited_once()
        service._graph_bfs_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_embeds_query_exactly_once(self) -> None:
        """Single-embed: the query is embedded ONCE and shared by both vector legs."""
        service, _mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("test query", self.PROJECT_ID, limit=20)

        # Exactly one embed call for the whole search — both vector legs
        # receive the same precomputed vector.
        service._embed_query.assert_awaited_once_with("test query", self.PROJECT_ID)
        embedding = service._embed_query.return_value
        service._vector_search_episodes.assert_awaited_once_with(
            embedding,
            self.PROJECT_ID,
            20,
        )
        service._vector_search_facts.assert_awaited_once_with(
            embedding,
            self.PROJECT_ID,
            20,
        )

    @pytest.mark.asyncio
    async def test_hybrid_search_episode_vector_leg_failure(self) -> None:
        """Single episode-vector failure degrades — surviving legs still merge."""
        service, mock_db = self._make_service()

        # Failing leg returns nothing; every other leg returns data.
        service._vector_search_episodes = AsyncMock(
            side_effect=RuntimeError("pgvector down"),
        )
        service._vector_search_facts = AsyncMock(
            return_value=[self._make_item("vf1", 0.85)]
        )
        service._bm25_search_episodes = AsyncMock(
            return_value=[self._make_item("b1", 0.8)]
        )
        service._bm25_search_facts = AsyncMock(
            return_value=[self._make_item("bf1", 0.75)]
        )
        service._graph_bfs_search = AsyncMock(
            return_value=[self._make_item("e1", 1.0, distance=0.5)]
        )

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "episode_vector" in result["failed_legs"]
        # Surviving legs still contribute merged results.
        assert result["facts"]
        assert result["entities"]
        # The failed leg wrapped + rolled back the shared session.
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_fact_vector_leg_failure(self) -> None:
        """Single fact-vector failure degrades — surviving legs still merge."""
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(
            return_value=[self._make_item("v1", 0.9)]
        )
        service._vector_search_facts = AsyncMock(
            side_effect=RuntimeError("pgvector down on facts"),
        )
        service._bm25_search_episodes = AsyncMock(
            return_value=[self._make_item("b1", 0.8)]
        )
        service._bm25_search_facts = AsyncMock(
            return_value=[self._make_item("bf1", 0.75)]
        )
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "fact_vector" in result["failed_legs"]
        assert result["episodes"]
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_episode_bm25_leg_failure(self) -> None:
        """Single episode-BM25 failure degrades — vector legs still merge."""
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(
            return_value=[self._make_item("v1", 0.9)]
        )
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(
            side_effect=RuntimeError("tsquery parse error"),
        )
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "episode_bm25" in result["failed_legs"]
        # The vector episode leg still merged.
        assert result["episodes"]
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_fact_bm25_leg_failure(self) -> None:
        """Single fact-BM25 failure degrades — vector fact legs still merge."""
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(
            return_value=[self._make_item("vf1", 0.85)]
        )
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(
            side_effect=RuntimeError("tsquery parse error on facts"),
        )
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "fact_bm25" in result["failed_legs"]
        assert result["facts"]
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_graph_bfs_searchleg_failed_error_passthrough(
        self,
    ) -> None:
        """A ``SearchLegFailedError`` from graph_bfs degrades, unwrapped.

        The handler catches ``SearchLegFailedError`` from the leg, records
        ``graph_bfs`` in ``failed_legs`` and merges the surviving legs —
        it never propagates. The passthrough skips the generic handler,
        so no rollback or extra logging wraps it.
        """
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(
            return_value=[self._make_item("v1", 0.9)]
        )
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(
            side_effect=SearchLegFailedError(
                leg_name="graph_bfs",
                original_error="Already a SearchLegFailedError from downstream",
            ),
        )

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "graph_bfs" in result["failed_legs"]
        assert result["episodes"]
        # Direct passthrough — not wrapped by the generic handler.
        mock_db.rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_hybrid_search_graph_bfs_leg_failure(self) -> None:
        """Graph BFS failure degrades — vector/BM25 legs still merge."""
        service, mock_db = self._make_service()

        # Mock earlier legs to succeed
        service._vector_search_episodes = AsyncMock(
            return_value=[self._make_item("v1", 0.9)]
        )
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(
            side_effect=RuntimeError("graph backend timeout"),
        )

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "graph_bfs" in result["failed_legs"]
        assert result["episodes"]
        assert result["entities"] == []
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_embed_query_failure_raises(self) -> None:
        """An embed failure degrades both vector legs — BM25/graph still run.

        The query is embedded once upstream; its failure marks
        ``episode_vector`` + ``fact_vector`` degraded while keyword and
        graph legs still execute and merge.
        """
        service, mock_db = self._make_service()
        service._embed_query = AsyncMock(
            side_effect=SearchLegFailedError(
                leg_name="embedding",
                original_error="embedding API timeout",
            ),
        )
        service._vector_search_episodes = AsyncMock()
        service._vector_search_facts = AsyncMock()
        service._bm25_search_episodes = AsyncMock(
            return_value=[self._make_item("b1", 0.8)]
        )
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "episode_vector" in result["failed_legs"]
        assert "fact_vector" in result["failed_legs"]
        # Keyword legs still ran and merged — no silent empty return.
        assert result["episodes"]
        service._bm25_search_episodes.assert_awaited_once()
        service._bm25_search_facts.assert_awaited_once()
        service._graph_bfs_search.assert_awaited_once()
        # Vector legs never execute without an embedding to search with.
        service._vector_search_episodes.assert_not_awaited()
        service._vector_search_facts.assert_not_awaited()
        mock_db.rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_hybrid_search_embed_query_failure_discards_ready_legs(
        self,
    ) -> None:
        """Embed failure keeps BM25/graph results — partial, not raised.

        Even with every leg mocked to return data, an embed failure
        degrades to the BM25/graph subset instead of raising.
        """
        service, _mock_db = self._make_service()
        service._embed_query = AsyncMock(
            side_effect=SearchLegFailedError(
                leg_name="embedding",
                original_error="dimension mismatch",
            ),
        )
        service._vector_search_episodes = AsyncMock(
            return_value=[self._make_item("v1", 0.9)]
        )
        service._vector_search_facts = AsyncMock(
            return_value=[self._make_item("vf1", 0.85)]
        )
        service._bm25_search_episodes = AsyncMock(
            return_value=[self._make_item("b1", 0.8)]
        )
        service._bm25_search_facts = AsyncMock(
            return_value=[self._make_item("bf1", 0.75)]
        )
        service._graph_bfs_search = AsyncMock(return_value=[self._make_item("e1", 1.0)])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "episode_vector" in result["failed_legs"]
        assert "fact_vector" in result["failed_legs"]
        # BM25/graph data survives the embed failure.
        assert result["episodes"]
        assert result["facts"]
        assert result["entities"]

    @pytest.mark.asyncio
    async def test_hybrid_search_vector_leg_failure_discards_other_leg_results(
        self,
    ) -> None:
        """A failing vector leg keeps survivors — partial return, no raise.

        The fact/BM25/graph legs all return data and the episode vector
        leg fails: the search degrades with the survivors merged instead
        of raising ``SearchLegFailedError``.
        """
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(
            side_effect=RuntimeError("pgvector down"),
        )
        service._vector_search_facts = AsyncMock(
            return_value=[self._make_item("vf1", 0.85)]
        )
        service._bm25_search_episodes = AsyncMock(
            return_value=[self._make_item("b1", 0.8)]
        )
        service._bm25_search_facts = AsyncMock(
            return_value=[self._make_item("bf1", 0.75)]
        )
        service._graph_bfs_search = AsyncMock(return_value=[self._make_item("e1", 1.0)])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "episode_vector" in result["failed_legs"]
        assert result["facts"]
        assert result["entities"]
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_all_legs_fail_raises(self) -> None:
        """All five legs failing raises ``SearchLegFailedError`` (leg=all)."""
        service, _mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(
            side_effect=RuntimeError("pgvector down"),
        )
        service._vector_search_facts = AsyncMock(
            side_effect=RuntimeError("pgvector down"),
        )
        service._bm25_search_episodes = AsyncMock(
            side_effect=RuntimeError("tsquery down"),
        )
        service._bm25_search_facts = AsyncMock(
            side_effect=RuntimeError("tsquery down"),
        )
        service._graph_bfs_search = AsyncMock(
            side_effect=RuntimeError("graph down"),
        )

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert exc_info.value.detail.get("leg") == "all"

    @pytest.mark.asyncio
    async def test_hybrid_search_empty_query(self) -> None:
        """Empty query string does not cause errors — legs handle it."""
        service, _mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("", self.PROJECT_ID, limit=20)

        assert result["episodes"] == []
        assert result["facts"] == []
        assert result["entities"] == []
        assert result["total_items"] == 0

    @pytest.mark.asyncio
    async def test_hybrid_search_all_legs_empty(self) -> None:
        """All five legs return empty results — total_items=0 and empty lists."""
        service, _mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("test query", self.PROJECT_ID, limit=20)

        assert result["episodes"] == []
        assert result["facts"] == []
        assert result["entities"] == []
        assert result["total_items"] == 0
        assert result["source_counts"]["episodes"]["vector"] == 0
        assert result["source_counts"]["episodes"]["bm25"] == 0
        assert result["source_counts"]["facts"]["vector"] == 0
        assert result["source_counts"]["facts"]["bm25"] == 0
        assert result["source_counts"]["entities"]["graph_bfs"] == 0

    @pytest.mark.asyncio
    async def test_hybrid_search_with_reranker(self) -> None:
        """When a reranker is configured, rerank is called on episodes and facts."""
        mock_reranker = AsyncMock()
        mock_reranker.rerank.return_value = []
        mock_reranker.backend_name = "test"

        mock_db = AsyncMock()
        service = HybridRetriever(
            db=mock_db,
            org_id=self.ORG_ID,
            reranker=mock_reranker,
        )
        service._embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        # Reranker was called for both episodes and facts
        assert mock_reranker.rerank.await_count == 2
        assert result["episodes"] == []
        assert result["facts"] == []

    @pytest.mark.asyncio
    async def test_hybrid_search_uses_reranker_retrieval_limit(self) -> None:
        """With reranker, the retrieval limit passed to each leg is max(limit, rerank_top_k)."""
        mock_reranker = AsyncMock()
        mock_reranker.rerank.return_value = []
        mock_reranker.backend_name = "test"

        mock_org_config = MagicMock()
        mock_org_config.reranker_top_k = 75
        mock_org_config.reranker_top_n = 10
        mock_org_config.to_llm_config_dict.return_value = None

        mock_db = AsyncMock()
        service = HybridRetriever(
            db=mock_db,
            org_id=self.ORG_ID,
            reranker=mock_reranker,
            org_config=mock_org_config,
        )
        service._embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        limit = 5
        await service.hybrid_search("query", self.PROJECT_ID, limit=limit)

        # With reranker_top_k=75 and limit=5, retrieval_limit = max(5, 75) = 75
        expected_limit = 75
        # Single-embed: both vector legs receive the SAME precomputed vector
        # (the ``_embed_query`` stub's return value), not the query string.
        embedding = [0.1, 0.2, 0.3]
        service._vector_search_episodes.assert_awaited_once_with(
            embedding,
            self.PROJECT_ID,
            expected_limit,
        )
        service._vector_search_facts.assert_awaited_once_with(
            embedding,
            self.PROJECT_ID,
            expected_limit,
        )
        service._bm25_search_episodes.assert_awaited_once_with(
            "query",
            self.PROJECT_ID,
            expected_limit,
        )
        service._bm25_search_facts.assert_awaited_once_with(
            "query",
            self.PROJECT_ID,
            expected_limit,
        )

    @pytest.mark.asyncio
    async def test_hybrid_search_with_reranker_failure(self) -> None:
        """Reranker failure degrades to unreranked RRF order — no raise."""
        mock_reranker = AsyncMock()
        mock_reranker.rerank.side_effect = RuntimeError("reranker model OOM")
        mock_reranker.backend_name = "test"

        mock_db = AsyncMock()
        service = HybridRetriever(
            db=mock_db,
            org_id=self.ORG_ID,
            reranker=mock_reranker,
        )
        service._embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])

        service._vector_search_episodes = AsyncMock(
            return_value=[self._make_item("a", 0.9)]
        )
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        assert "reranker" in result["failed_legs"]
        # Retrieval legs already succeeded — their results survive unreranked.
        assert result["episodes"]

    @pytest.mark.asyncio
    async def test_hybrid_search_rerank_failure(self) -> None:
        """Reranker crash degrades — ``failed_legs`` carries ``reranker``."""
        mock_reranker = AsyncMock()
        mock_reranker.rerank.side_effect = RuntimeError("reranker crashed")
        mock_reranker.backend_name = "test"

        mock_db = AsyncMock()
        service = HybridRetriever(
            db=mock_db,
            org_id=self.ORG_ID,
            reranker=mock_reranker,
        )
        service._embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])

        service._vector_search_episodes = AsyncMock(
            return_value=[self._make_item("a", 0.9)]
        )
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        result = await service.hybrid_search("query", self.PROJECT_ID)

        assert result["degraded"] is True
        # The rerank failure is recorded by leg name, not raised.
        assert "reranker" in result["failed_legs"]
        assert result["episodes"]


@pytest.mark.unit
class TestEmbedQuery:
    """Tests for ``HybridRetriever._embed_query`` — embedding generation."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

    @staticmethod
    def _make_item(item_id: str, score: float, **extra: Any) -> dict[str, Any]:
        return {"id": item_id, "score": score, **extra}

    def _make_service(self) -> tuple[HybridRetriever, AsyncMock]:
        mock_db = AsyncMock()
        service = HybridRetriever(db=mock_db, org_id=self.ORG_ID)
        return service, mock_db

    @pytest.mark.asyncio
    async def test_embed_query_success(self) -> None:
        """Happy path: returns the vector from the local embedder."""
        embedding = [0.1] * 768  # canonical dim (nomic-embed-text-v1.5)
        embedder = AsyncMock(return_value=[embedding])

        with patch("core.embeddings.embed_query", embedder):
            service, _ = self._make_service()
            result = await service._embed_query("test query")

        assert result == embedding
        embedder.assert_awaited_once_with(["test query"])

    @pytest.mark.asyncio
    async def test_embed_query_uses_query_prefix(self) -> None:
        """The query leg must use ``embed_query``, never ``embed_passage``.

        ``nomic-embed-text-v1.5`` is trained with asymmetric search/document
        prefixes.  Using the passage prefix here would still return 768 valid
        floats and silently degrade recall, so this is pinned explicitly.
        """
        passage = AsyncMock(return_value=[[0.1] * 768])
        query = AsyncMock(return_value=[[0.2] * 768])

        with (
            patch("core.embeddings.embed_query", query),
            patch("core.embeddings.embed_passage", passage),
        ):
            service, _ = self._make_service()
            result = await service._embed_query("test query")

        assert result == [0.2] * 768
        query.assert_awaited_once()
        passage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_embed_query_empty_embeddings(self) -> None:
        """Embedder returns no vectors — SearchLegFailedError is raised."""
        embedder = AsyncMock(return_value=[])

        with patch("core.embeddings.embed_query", embedder):
            service, _ = self._make_service()
            with pytest.raises(SearchLegFailedError) as exc_info:
                await service._embed_query("test")

        assert exc_info.value.detail.get("leg") == "embedding"

    @pytest.mark.asyncio
    async def test_embed_query_failure(self) -> None:
        """The embedder raises — SearchLegFailedError is raised."""
        embedder = AsyncMock(side_effect=RuntimeError("onnx inference timeout"))

        with patch("core.embeddings.embed_query", embedder):
            service, _ = self._make_service()
            with pytest.raises(SearchLegFailedError) as exc_info:
                await service._embed_query("test")

        assert exc_info.value.detail.get("leg") == "embedding"

    @pytest.mark.asyncio
    async def test_embed_query_tracks_embedding_dim(self) -> None:
        """``_last_query_embedding_dim`` is set to the length of the embedding vector."""
        embedding = [0.1] * 768  # canonical dim (nomic-embed-text-v1.5)
        embedder = AsyncMock(return_value=[embedding])

        with patch("core.embeddings.embed_query", embedder):
            service, _ = self._make_service()
            assert service._last_query_embedding_dim is None

            await service._embed_query("test")

        assert service._last_query_embedding_dim == 768

    @pytest.mark.asyncio
    async def test_embed_query_wrong_dim_rejected(self) -> None:
        """A wrong-dim vector is refused, never searched with."""
        embedder = AsyncMock(return_value=[[0.1] * 512])

        with patch("core.embeddings.embed_query", embedder):
            service, _ = self._make_service()
            with pytest.raises(SearchLegFailedError) as exc_info:
                await service._embed_query("test")

        assert exc_info.value.detail.get("leg") == "embedding"
        # The wrapper keeps the original reason in detail, not in str().
        assert "len 512" in exc_info.value.detail["original_error"]

    @pytest.mark.asyncio
    async def test_embed_query_ignores_org_config(self) -> None:
        """Org config is irrelevant to embedding — no provider is resolved.

        Retained from the frozen-model era: the guarantee is now stronger.
        An org config carrying legacy ``embedding_*`` keys cannot influence
        which model runs, because there is no provider to route to.
        """
        mock_org_config = MagicMock()
        mock_org_config.to_llm_config_dict.return_value = {"provider": "openai"}
        mock_org_config.reranker_top_k = None
        mock_org_config.reranker_top_n = None

        embedding = [0.1] * 768  # canonical dim (nomic-embed-text-v1.5)
        embedder = AsyncMock(return_value=[embedding])
        service = HybridRetriever(
            db=AsyncMock(),
            org_id=self.ORG_ID,
            org_config=mock_org_config,
        )

        with (
            patch("core.embeddings.embed_query", embedder),
            patch("core.llm.resolve_backend") as mock_resolve,
        ):
            result = await service._embed_query("test")

        assert result == embedding
        embedder.assert_awaited_once_with(["test"])
        mock_resolve.assert_not_called()


@pytest.mark.unit
class TestVectorSearch:
    """Tests for ``HybridRetriever._vector_search_episodes`` and ``_vector_search_facts``."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

    @staticmethod
    def _make_item(item_id: str, score: float, **extra: Any) -> dict[str, Any]:
        return {"id": item_id, "score": score, **extra}

    def _make_service(self) -> tuple[HybridRetriever, AsyncMock]:
        mock_db = AsyncMock()
        service = HybridRetriever(db=mock_db, org_id=self.ORG_ID)
        return service, mock_db

    @pytest.mark.asyncio
    async def test_vector_search_episodes_success(self) -> None:
        """Episode vector search returns results from ``_execute_ranked_query``.

        The query embedding is a precomputed parameter now — the leg no
        longer embeds internally (single-embed lives in ``hybrid_search``).
        """
        service, _ = self._make_service()
        mock_results = [
            {
                "id": "ep1",
                "score": 0.95,
                "content": "test episode",
                "role": "assistant",
            },
        ]

        service._execute_ranked_query = AsyncMock(return_value=mock_results)

        results = await service._vector_search_episodes(
            [0.1, 0.2, 0.3],
            self.PROJECT_ID,
            limit=20,
        )

        assert results == mock_results
        service._execute_ranked_query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vector_search_facts_success(self) -> None:
        """Fact vector search returns results from ``_execute_ranked_query``."""
        service, _ = self._make_service()
        mock_results = [
            {
                "id": "f1",
                "score": 0.92,
                "content": "test fact",
                "subject": "S",
                "predicate": "P",
            },
        ]

        service._execute_ranked_query = AsyncMock(return_value=mock_results)

        results = await service._vector_search_facts(
            [0.1, 0.2, 0.3],
            self.PROJECT_ID,
            limit=20,
        )

        assert results == mock_results
        service._execute_ranked_query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vector_search_episodes_empty_results(self) -> None:
        """Episode vector search returns empty list when no matches found."""
        service, _ = self._make_service()

        service._execute_ranked_query = AsyncMock(return_value=[])

        results = await service._vector_search_episodes(
            [0.1, 0.2, 0.3],
            self.PROJECT_ID,
            limit=20,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_vector_search_facts_empty_results(self) -> None:
        """Fact vector search returns empty list when no matches found."""
        service, _ = self._make_service()

        service._execute_ranked_query = AsyncMock(return_value=[])

        results = await service._vector_search_facts(
            [0.1, 0.2, 0.3],
            self.PROJECT_ID,
            limit=20,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_vector_search_facts_include_validity_keys(self) -> None:
        """Fact vector leg returns dicts carrying valid_from, valid_to, invalid_at."""
        service, _ = self._make_service()
        mock_results = [
            {
                "id": "f1",
                "score": 0.92,
                "content": "test fact",
                "subject": "S",
                "predicate": "P",
                "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
                "valid_to": datetime(2026, 6, 1, tzinfo=UTC),
                "invalid_at": None,
            },
        ]

        service._execute_ranked_query = AsyncMock(return_value=mock_results)

        results = await service._vector_search_facts(
            [0.1, 0.2, 0.3],
            self.PROJECT_ID,
            limit=20,
        )

        assert len(results) == 1
        assert results[0]["valid_from"] == datetime(2026, 1, 1, tzinfo=UTC)
        assert results[0]["valid_to"] == datetime(2026, 6, 1, tzinfo=UTC)
        assert results[0]["invalid_at"] is None


@pytest.mark.unit
class TestGraphBFSSearch:
    """Tests for ``HybridRetriever._graph_bfs_search`` — graph traversal and dedup."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

    @staticmethod
    def _make_item(item_id: str, score: float, **extra: Any) -> dict[str, Any]:
        return {"id": item_id, "score": score, **extra}

    def _make_service(
        self,
        graph_backends: list[Any] | None = None,
    ) -> HybridRetriever:
        mock_db = AsyncMock()
        return HybridRetriever(
            db=mock_db,
            org_id=self.ORG_ID,
            graph_backends=graph_backends,
        )

    @pytest.mark.asyncio
    async def test_graph_bfs_no_backends(self) -> None:
        """No graph backends configured → returns empty list."""
        service = self._make_service(graph_backends=[])
        result = await service._graph_bfs_search("query", self.PROJECT_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_graph_bfs_multiple_backends(self) -> None:
        """Multiple backends return entities that are merged together."""
        backend1 = AsyncMock()
        backend1.retrieve_graph.return_value = [
            {"id": "1", "name": "Entity A", "distance": 0.1},
            {"id": "2", "name": "Entity B", "distance": 0.3},
        ]
        backend2 = AsyncMock()
        backend2.retrieve_graph.return_value = [
            {"id": "3", "name": "Entity C", "distance": 0.2},
        ]

        service = self._make_service(graph_backends=[backend1, backend2])
        result = await service._graph_bfs_search("query", self.PROJECT_ID)

        # All 3 unique entities returned, sorted by distance
        assert len(result) == 3
        assert [r["id"] for r in result] == ["1", "3", "2"]

    @pytest.mark.asyncio
    async def test_graph_bfs_deduplicates_by_id(self) -> None:
        """Duplicate entities from multiple backends are deduplicated by ``id``."""
        backend1 = AsyncMock()
        backend1.retrieve_graph.return_value = [
            {"id": "1", "name": "Entity A", "distance": 0.1},
        ]
        backend2 = AsyncMock()
        backend2.retrieve_graph.return_value = [
            {"id": "1", "name": "Entity A", "distance": 0.2},
        ]

        service = self._make_service(graph_backends=[backend1, backend2])
        result = await service._graph_bfs_search("query", self.PROJECT_ID)

        assert len(result) == 1
        assert result[0]["id"] == "1"
        # First occurrence wins — distance from backend1
        assert result[0]["distance"] == 0.1

    @pytest.mark.asyncio
    async def test_graph_bfs_sorts_by_distance(self) -> None:
        """Results are returned sorted by distance ascending (closest match first)."""
        backend = AsyncMock()
        backend.retrieve_graph.return_value = [
            {"id": "3", "distance": 0.5},
            {"id": "1", "distance": 0.1},
            {"id": "2", "distance": 0.3},
        ]

        service = self._make_service(graph_backends=[backend])
        result = await service._graph_bfs_search("query", self.PROJECT_ID)

        assert [r["id"] for r in result] == ["1", "2", "3"]
        assert [r["distance"] for r in result] == [0.1, 0.3, 0.5]

    @pytest.mark.asyncio
    async def test_graph_bfs_respects_max_results(self) -> None:
        """Only ``MAX_BFS_RESULTS`` items are returned, no more."""
        backend = AsyncMock()
        backend.retrieve_graph.return_value = [
            {"id": str(i), "distance": 0.01 * i} for i in range(100)
        ]

        service = self._make_service(graph_backends=[backend])
        result = await service._graph_bfs_search("query", self.PROJECT_ID)

        assert len(result) == MAX_BFS_RESULTS


@pytest.mark.unit
class TestExecuteRankedQuery:
    """Tests for ``HybridRetriever._execute_ranked_query`` — row mapping and score rounding."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

    @staticmethod
    def _make_item(item_id: str, score: float, **extra: Any) -> dict[str, Any]:
        return {"id": item_id, "score": score, **extra}

    def _make_service(self) -> tuple[HybridRetriever, AsyncMock]:
        mock_db = AsyncMock()
        service = HybridRetriever(db=mock_db, org_id=self.ORG_ID)
        return service, mock_db

    @pytest.mark.asyncio
    async def test_execute_ranked_query_returns_dicts(self) -> None:
        """``_db.execute`` rows with ``_mapping`` are converted to regular dicts."""
        service, mock_db = self._make_service()

        mock_row = MagicMock()
        mock_row._mapping = {"id": "1", "score": 0.95, "content": "test"}
        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        mock_db.execute.return_value = mock_result

        stmt = MagicMock()  # not used — db.execute is mocked
        results = await service._execute_ranked_query(stmt)

        assert len(results) == 1
        assert results[0] == {"id": "1", "score": 0.95, "content": "test"}

    @pytest.mark.asyncio
    async def test_execute_ranked_query_rounds_scores(self) -> None:
        """Score is rounded to 6 decimal places to avoid floating-point noise."""
        service, mock_db = self._make_service()

        mock_row = MagicMock()
        mock_row._mapping = {"id": "1", "score": 0.123456789}
        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        mock_db.execute.return_value = mock_result

        results = await service._execute_ranked_query(MagicMock())

        assert results[0]["score"] == round(0.123456789, 6)

    @pytest.mark.asyncio
    async def test_execute_ranked_query_null_score_handled(self) -> None:
        """When score is None, it remains None (not rounded)."""
        service, mock_db = self._make_service()

        mock_row = MagicMock()
        mock_row._mapping = {"id": "1", "score": None}
        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        mock_db.execute.return_value = mock_result

        results = await service._execute_ranked_query(MagicMock())

        assert results[0]["score"] is None

    @pytest.mark.asyncio
    async def test_execute_ranked_query_passes_validity_keys(self) -> None:
        """Validity columns in ``row._mapping`` flow through into result dicts."""
        service, mock_db = self._make_service()

        mock_row = MagicMock()
        mock_row._mapping = {
            "id": "1",
            "score": 0.95,
            "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
            "valid_to": datetime(2026, 6, 1, tzinfo=UTC),
            "invalid_at": None,
        }
        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        mock_db.execute.return_value = mock_result

        results = await service._execute_ranked_query(MagicMock())

        assert len(results) == 1
        assert results[0]["valid_from"] == datetime(2026, 1, 1, tzinfo=UTC)
        assert results[0]["valid_to"] == datetime(2026, 6, 1, tzinfo=UTC)
        assert results[0]["invalid_at"] is None


@pytest.mark.unit
class TestBM25Search:
    """Tests for ``HybridRetriever._bm25_search_facts`` — the BM25 fact leg."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

    def _make_service(self) -> tuple[HybridRetriever, AsyncMock]:
        mock_db = AsyncMock()
        service = HybridRetriever(db=mock_db, org_id=self.ORG_ID)
        return service, mock_db

    @pytest.mark.asyncio
    async def test_bm25_search_facts_include_validity_keys(self) -> None:
        """Fact BM25 leg returns dicts carrying valid_from, valid_to, invalid_at."""
        from unittest.mock import patch

        service, _ = self._make_service()
        mock_results = [
            {
                "id": "f1",
                "score": 0.5,
                "content": "test fact",
                "subject": "S",
                "predicate": "P",
                "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
                "valid_to": datetime(2026, 6, 1, tzinfo=UTC),
                "invalid_at": None,
            },
        ]

        with patch(
            "services.hybrid_retriever.FactRepository",
        ) as mock_repo_cls:
            mock_repo_cls.return_value.search_by_bm25 = AsyncMock(
                return_value=mock_results
            )
            results = await service._bm25_search_facts(
                "test",
                self.PROJECT_ID,
                limit=20,
            )
            mock_repo_cls.return_value.search_by_bm25.assert_awaited_once_with(
                query="test",
                project_id=self.PROJECT_ID,
                org_id=self.ORG_ID,
                limit=20,
                effective_at=None,
            )

        assert len(results) == 1
        assert results[0]["valid_from"] == datetime(2026, 1, 1, tzinfo=UTC)
        assert results[0]["valid_to"] == datetime(2026, 6, 1, tzinfo=UTC)
        assert results[0]["invalid_at"] is None
