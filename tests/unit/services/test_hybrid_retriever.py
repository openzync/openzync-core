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
        service._embed_query.assert_awaited_once_with("test query")
        embedding = service._embed_query.return_value
        service._vector_search_episodes.assert_awaited_once_with(
            embedding, self.PROJECT_ID, 20,
        )
        service._vector_search_facts.assert_awaited_once_with(
            embedding, self.PROJECT_ID, 20,
        )

    @pytest.mark.asyncio
    async def test_hybrid_search_episode_vector_leg_failure(self) -> None:
        """When episode vector leg fails, SearchLegFailedError is raised."""
        service, mock_db = self._make_service()

        # Make the first leg raise
        service._vector_search_episodes = AsyncMock(
            side_effect=RuntimeError("pgvector down"),
        )

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert "episode_vector" in str(exc_info.value)
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_fact_vector_leg_failure(self) -> None:
        """When fact vector leg fails, SearchLegFailedError is raised."""
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(
            side_effect=RuntimeError("pgvector down on facts"),
        )

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert "fact_vector" in str(exc_info.value)
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_episode_bm25_leg_failure(self) -> None:
        """When episode BM25 leg fails, SearchLegFailedError is raised."""
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(
            side_effect=RuntimeError("tsquery parse error"),
        )

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert "episode_bm25" in str(exc_info.value)
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_fact_bm25_leg_failure(self) -> None:
        """When fact BM25 leg fails, SearchLegFailedError is raised."""
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(
            side_effect=RuntimeError("tsquery parse error on facts"),
        )

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert "fact_bm25" in str(exc_info.value)
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_graph_bfs_searchleg_failed_error_passthrough(
        self,
    ) -> None:
        """When graph_bfs_search raises SearchLegFailedError directly, it is re-raised.

        The handler at line 191 catches ``SearchLegFailedError`` before the
        generic ``Exception`` handler — the error propagates as-is without
        logging, rollback, or wrapping.
        """
        service, mock_db = self._make_service()

        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(
            side_effect=SearchLegFailedError(
                leg_name="graph_bfs",
                original_error="Already a SearchLegFailedError from downstream",
            ),
        )

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert "graph_bfs" in str(exc_info.value)
        # db.rollback should NOT be called — it's a direct passthrough,
        # not a wrapped exception from the generic handler
        mock_db.rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_hybrid_search_graph_bfs_leg_failure(self) -> None:
        """When graph BFS leg fails, SearchLegFailedError is raised."""
        service, mock_db = self._make_service()

        # Mock earlier legs to succeed
        service._vector_search_episodes = AsyncMock(return_value=[])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(
            side_effect=RuntimeError("graph backend timeout"),
        )

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert "graph_bfs" in str(exc_info.value)
        mock_db.rollback.assert_awaited_once()

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
        mock_org_config.embedding_backend = None
        mock_org_config.embedding_model = None
        mock_org_config.embedding_dim = None
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
            embedding, self.PROJECT_ID, expected_limit,
        )
        service._vector_search_facts.assert_awaited_once_with(
            embedding, self.PROJECT_ID, expected_limit,
        )
        service._bm25_search_episodes.assert_awaited_once_with(
            "query", self.PROJECT_ID, expected_limit,
        )
        service._bm25_search_facts.assert_awaited_once_with(
            "query", self.PROJECT_ID, expected_limit,
        )

    @pytest.mark.asyncio
    async def test_hybrid_search_with_reranker_failure(self) -> None:
        """When reranker fails, SearchLegFailedError is raised."""
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

        service._vector_search_episodes = AsyncMock(return_value=[self._make_item("a", 0.9)])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert "reranker" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_hybrid_search_rerank_failure(self) -> None:
        """Reranker failure raises SearchLegFailedError with ``reranker`` leg name."""
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

        service._vector_search_episodes = AsyncMock(return_value=[self._make_item("a", 0.9)])
        service._vector_search_facts = AsyncMock(return_value=[])
        service._bm25_search_episodes = AsyncMock(return_value=[])
        service._bm25_search_facts = AsyncMock(return_value=[])
        service._graph_bfs_search = AsyncMock(return_value=[])

        with pytest.raises(SearchLegFailedError) as exc_info:
            await service.hybrid_search("query", self.PROJECT_ID)

        assert "reranker" in str(exc_info.value)
        # Verify the leg detail is correct
        assert exc_info.value.detail.get("leg") == "reranker"


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
        """Happy path: returns the embedding vector from the LLM backend."""
        mock_backend = AsyncMock()
        mock_backend.embed = AsyncMock(return_value=MagicMock(embeddings=[[0.1, 0.2, 0.3]]))

        mock_resolve = AsyncMock(return_value=mock_backend)
        with patch("core.llm.resolve_backend", mock_resolve):
            service, _ = self._make_service()
            result = await service._embed_query("test query")

        assert result == [0.1, 0.2, 0.3]
        mock_resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embed_query_empty_embeddings(self) -> None:
        """Backend returns no embeddings — SearchLegFailedError is raised."""
        mock_backend = AsyncMock()
        mock_backend.embed = AsyncMock(return_value=MagicMock(embeddings=[]))

        mock_resolve = AsyncMock(return_value=mock_backend)
        with patch("core.llm.resolve_backend", mock_resolve):
            service, _ = self._make_service()
            with pytest.raises(SearchLegFailedError) as exc_info:
                await service._embed_query("test")

        assert exc_info.value.detail.get("leg") == "embedding"

    @pytest.mark.asyncio
    async def test_embed_query_failure(self) -> None:
        """Backend.embed raises an exception — SearchLegFailedError is raised."""
        mock_backend = AsyncMock()
        mock_backend.embed = AsyncMock(side_effect=RuntimeError("embedding API timeout"))

        mock_resolve = AsyncMock(return_value=mock_backend)
        with patch("core.llm.resolve_backend", mock_resolve):
            service, _ = self._make_service()
            with pytest.raises(SearchLegFailedError) as exc_info:
                await service._embed_query("test")

        assert exc_info.value.detail.get("leg") == "embedding"

    @pytest.mark.asyncio
    async def test_embed_query_tracks_embedding_dim(self) -> None:
        """``_last_query_embedding_dim`` is set to the length of the embedding vector."""
        mock_backend = AsyncMock()
        mock_backend.embed = AsyncMock(
            return_value=MagicMock(embeddings=[[0.1, 0.2, 0.3, 0.4, 0.5]]),
        )

        mock_resolve = AsyncMock(return_value=mock_backend)
        with patch("core.llm.resolve_backend", mock_resolve):
            service, _ = self._make_service()
            assert service._last_query_embedding_dim is None

            await service._embed_query("test")

        assert service._last_query_embedding_dim == 5

    @pytest.mark.asyncio
    async def test_embed_query_with_org_config(self) -> None:
        """With org_config, the provider and model are passed to resolve_backend."""
        mock_org_config = MagicMock()
        mock_org_config.to_llm_config_dict.return_value = {"provider": "openai"}
        mock_org_config.embedding_backend = "openai"
        mock_org_config.embedding_model = "text-embedding-3-small"
        mock_org_config.embedding_dim = 1536
        mock_org_config.reranker_top_k = None
        mock_org_config.reranker_top_n = None

        mock_backend = AsyncMock()
        mock_backend.embed = AsyncMock(return_value=MagicMock(embeddings=[[0.1, 0.2, 0.3]]))

        mock_db = AsyncMock()
        service = HybridRetriever(
            db=mock_db,
            org_id=self.ORG_ID,
            org_config=mock_org_config,
        )

        mock_resolve = AsyncMock(return_value=mock_backend)
        with patch("core.llm.resolve_backend", mock_resolve):
            result = await service._embed_query("test")

        assert result == [0.1, 0.2, 0.3]
        mock_resolve.assert_awaited_once_with(
            provider="openai",
            org_config={"provider": "openai"},
        )
        mock_backend.embed.assert_awaited_once_with(
            ["test"],
            model="text-embedding-3-small",
        )


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
            {"id": "ep1", "score": 0.95, "content": "test episode", "role": "assistant"},
        ]

        service._execute_ranked_query = AsyncMock(return_value=mock_results)

        results = await service._vector_search_episodes(
            [0.1, 0.2, 0.3], self.PROJECT_ID, limit=20,
        )

        assert results == mock_results
        service._execute_ranked_query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vector_search_facts_success(self) -> None:
        """Fact vector search returns results from ``_execute_ranked_query``."""
        service, _ = self._make_service()
        mock_results = [
            {"id": "f1", "score": 0.92, "content": "test fact", "subject": "S", "predicate": "P"},
        ]

        service._execute_ranked_query = AsyncMock(return_value=mock_results)

        results = await service._vector_search_facts(
            [0.1, 0.2, 0.3], self.PROJECT_ID, limit=20,
        )

        assert results == mock_results
        service._execute_ranked_query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vector_search_episodes_empty_results(self) -> None:
        """Episode vector search returns empty list when no matches found."""
        service, _ = self._make_service()

        service._execute_ranked_query = AsyncMock(return_value=[])

        results = await service._vector_search_episodes(
            [0.1, 0.2, 0.3], self.PROJECT_ID, limit=20,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_vector_search_facts_empty_results(self) -> None:
        """Fact vector search returns empty list when no matches found."""
        service, _ = self._make_service()

        service._execute_ranked_query = AsyncMock(return_value=[])

        results = await service._vector_search_facts(
            [0.1, 0.2, 0.3], self.PROJECT_ID, limit=20,
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
            [0.1, 0.2, 0.3], self.PROJECT_ID, limit=20,
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

        service._execute_ranked_query = AsyncMock(return_value=mock_results)

        results = await service._bm25_search_facts(
            "test", self.PROJECT_ID, limit=20,
        )

        assert len(results) == 1
        assert results[0]["valid_from"] == datetime(2026, 1, 1, tzinfo=UTC)
        assert results[0]["valid_to"] == datetime(2026, 6, 1, tzinfo=UTC)
        assert results[0]["invalid_at"] is None
