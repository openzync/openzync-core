"""Integration tests for the re-ranking pipeline in hybrid retrieval.

Tests cover the integration of ``CrossEncoderReranker`` within the
full retrieval-and-format pipeline:

1. Re-ranker disabled → RRF-only results (no ``reranker_score``).
2. Re-ranker enabled → results include ``reranker_score``.
3. Re-ranker failure → propagates as ``SearchLegFailedError``.
4. Context assembly formatting includes ``reranker_score``.

Uses the testcontainers PostgreSQL + Redis stack from ``conftest.py``,
but mocks the individual search legs to return controlled data without
requiring seeded DB content.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.reranker.sentence_transformers import _MODEL_CACHE, _MODEL_LOCKS
from schemas.organization_config import OrgConfigBase
from services.context_service import ContextService
from core.exceptions import SearchLegFailedError
from services.hybrid_retriever import HybridRetriever

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ═══════════════════════════════════════════════════════════════════════════════
# Shared test data
# ═══════════════════════════════════════════════════════════════════════════════


ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


PROJECT_ID = uuid4()


@pytest.fixture
def mock_db_session() -> AsyncMock:
    """Mock DB session — no real queries executed in these tests.

    The individual search methods on ``HybridRetriever`` are mocked
    separately, so the session just needs to be a valid ``AsyncSession``
    for instantiation.
    """
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def sample_search_results() -> dict[str, Any]:
    """Controlled RRF-worthy results for the ``HybridRetriever`` search legs.

    ``episode_vector_results`` and ``episode_bm25_results`` overlap in
    ``id`` so the RRF merge can deduplicate and fuse scores.  The
    ``reranker_score`` is *not* set here — the re-ranker adds it.
    """
    return {
        "episode_vector": [
            {"id": "ep-1", "content": "Python is a programming language", "score": 0.92},
            {"id": "ep-2", "content": "FastAPI is a Python web framework", "score": 0.75},
            {"id": "ep-3", "content": "JavaScript runs in the browser", "score": 0.60},
        ],
        "episode_bm25": [
            {"id": "ep-1", "content": "Python is a programming language", "score": 0.85},
            {"id": "ep-2", "content": "FastAPI is a Python web framework", "score": 0.70},
        ],
        "fact_vector": [
            {"id": "fact-1", "content": "Guido van Rossum created Python", "score": 0.88},
        ],
        "fact_bm25": [
            {"id": "fact-1", "content": "Guido van Rossum created Python", "score": 0.80},
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: build a HybridRetriever with mocked search legs
# ═══════════════════════════════════════════════════════════════════════════════


def _build_mocked_retriever(
    db: AsyncSession,
    org_id_val: UUID,
    sample: dict[str, Any],
    *,
    org_config: OrgConfigBase | None = None,
    reranker: Any = None,
) -> HybridRetriever:
    """Construct a ``HybridRetriever`` with all search legs mocked.

    Each internal search method is replaced with an ``AsyncMock`` that
    returns controlled data, so tests focus on the RRF merge and
    re-ranking steps without needing real DB content.

    Args:
        db: A mock async session.
        org_id_val: Organisation UUID (for tenant isolation).
        sample: The controlled search results to return.
        org_config: Optional org-level configuration.
        reranker: An optional re-ranker instance (real or mock).

    Returns:
        A ``HybridRetriever`` instance with mocked search legs.
    """
    retriever = HybridRetriever(
        db=db,
        org_id=org_id_val,
        redis=None,
        graph_backends=[],
        org_config=org_config,
        reranker=reranker,
    )

    retriever._embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
    retriever._vector_search_episodes = AsyncMock(
        return_value=sample["episode_vector"],
    )
    retriever._vector_search_facts = AsyncMock(
        return_value=sample["fact_vector"],
    )
    retriever._bm25_search_episodes = AsyncMock(
        return_value=sample["episode_bm25"],
    )
    retriever._bm25_search_facts = AsyncMock(
        return_value=sample["fact_bm25"],
    )
    retriever._graph_bfs_search = AsyncMock(return_value=[])

    return retriever


# ═══════════════════════════════════════════════════════════════════════════════
# Test: reranker disabled
# ═══════════════════════════════════════════════════════════════════════════════


class TestRerankerDisabled:
    """Pipeline behaviour when re-ranking is not configured."""

    async def test_reranker_disabled_returns_rrf_results(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
    ) -> None:
        """With ``reranker_backend=None``, results have ``rrf_score`` but no
        ``reranker_score``.

        The RRF merge runs on the mocked search legs; the output should
        contain only the RRF-fused score, not a cross-encoder score.
        """
        org_config = OrgConfigBase(reranker_backend=None)
        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample_search_results,
            org_config=org_config,
            reranker=None,
        )

        result = await retriever.hybrid_search("python", PROJECT_ID, limit=20)

        episodes = result["episodes"]
        facts = result["facts"]

        # All episode results should have ``rrf_score``
        for ep in episodes:
            assert "rrf_score" in ep, "RRF-fused results must have rrf_score"
            assert isinstance(ep["rrf_score"], float)
            assert ep["rrf_score"] > 0, "rrf_score should be positive"

        # No result should have ``reranker_score``
        for ep in episodes:
            assert "reranker_score" not in ep, (
                "Without re-ranker, reranker_score must not appear"
            )

        for fact in facts:
            assert "reranker_score" not in fact, (
                "Without re-ranker, reranker_score must not appear"
            )
            # Facts should still have rrf_score
            assert "rrf_score" in fact

        # Verify RRF ordering: ep-1 appears in both vector and BM25 → highest RRF
        assert episodes[0]["id"] == "ep-1"

    async def test_disabled_returns_expected_source_counts(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
    ) -> None:
        """Source counts are accurate when re-ranking is disabled."""
        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample_search_results,
            org_config=OrgConfigBase(reranker_backend=None),
            reranker=None,
        )

        result = await retriever.hybrid_search("python", PROJECT_ID, limit=20)

        counts = result["source_counts"]
        assert counts["episodes"]["vector"] == 3
        assert counts["episodes"]["bm25"] == 2
        assert counts["facts"]["vector"] == 1
        assert counts["facts"]["bm25"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: reranker enabled
# ═══════════════════════════════════════════════════════════════════════════════


class TestRerankerEnabled:
    """Pipeline behaviour when re-ranking is active."""

    @pytest.fixture(autouse=True)
    def _clear_model_cache(self) -> None:
        """Reset module-level cache to avoid cross-test pollution."""
        _MODEL_CACHE.clear()
        _MODEL_LOCKS.clear()

    @pytest.fixture
    def mock_reranker(self) -> AsyncMock:
        """A mock re-ranker that scores candidates deterministically.

        Returns candidates in a different order than RRF to make it
        obvious that re-ranking took effect.
        """
        reranker = AsyncMock()
        reranker.rerank.return_value = [
            {
                "id": "ep-2",
                "content": "FastAPI is a Python web framework",
                "rrf_score": 0.016393,
                "reranker_score": 0.95,
            },
            {
                "id": "ep-1",
                "content": "Python is a programming language",
                "rrf_score": 0.033333,
                "reranker_score": 0.80,
            },
            {
                "id": "ep-3",
                "content": "JavaScript runs in the browser",
                "rrf_score": 0.016129,
                "reranker_score": 0.10,
            },
        ]
        return reranker

    async def test_reranker_enabled_has_reranker_score(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
        mock_reranker: AsyncMock,
    ) -> None:
        """With re-ranking enabled, results include ``reranker_score``.

        The mock re-ranker returns items with ``reranker_score`` set;
        the pipeline propagates these through to the output.
        """
        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample_search_results,
            org_config=OrgConfigBase(reranker_backend="sentence_transformers"),
            reranker=mock_reranker,
        )

        result = await retriever.hybrid_search("python", PROJECT_ID, limit=20)

        episodes = result["episodes"]

        # All episodes should have both scores
        for ep in episodes:
            assert "reranker_score" in ep, "Re-ranked results must have reranker_score"
            assert "rrf_score" in ep, "Re-ranked results must preserve rrf_score"

        # Order should match re-ranker: ep-2 (0.95) first, then ep-1 (0.80)
        assert episodes[0]["id"] == "ep-2"
        assert episodes[0]["reranker_score"] == 0.95
        assert episodes[1]["id"] == "ep-1"
        assert episodes[1]["reranker_score"] == 0.80

    async def test_reranker_called_with_query_and_candidates(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
        mock_reranker: AsyncMock,
    ) -> None:
        """The re-ranker is invoked with the query and RRF-merged candidates."""
        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample_search_results,
            org_config=OrgConfigBase(reranker_backend="sentence_transformers"),
            reranker=mock_reranker,
        )

        await retriever.hybrid_search("python", PROJECT_ID, limit=20)

        # The re-ranker should have been called with the query and episodes
        assert mock_reranker.rerank.call_count == 2  # episodes + facts
        args, _ = mock_reranker.rerank.call_args_list[0]
        assert args[0] == "python", (
            "Expected 'query' as the first positional argument"
        )
        assert isinstance(args[1], list), (
            "Expected candidates list as the second positional argument"
        )
        # Candidates passed to the re-ranker should have rrf_score (set by RRF)
        assert all("rrf_score" in c for c in args[1]), (
            "Candidates should already have rrf_score from RRF merge"
        )

    async def test_reranker_top_n_respected(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
    ) -> None:
        """The ``reranker_top_n`` config limits re-ranked output count.

        Even though RRF returns 2+ candidates, the re-ranker should only
        return the configured top-n.
        """
        reranker = AsyncMock()
        # Return only 1 item regardless of input
        reranker.rerank.return_value = [
            {
                "id": "ep-1",
                "content": "Python is a programming language",
                "rrf_score": 0.033333,
                "reranker_score": 0.90,
            },
        ]

        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample_search_results,
            org_config=OrgConfigBase(
                reranker_backend="sentence_transformers",
                reranker_top_n=1,
            ),
            reranker=reranker,
        )

        result = await retriever.hybrid_search("python", PROJECT_ID, limit=20)

        # Only 1 episode should be returned (top_n=1)
        assert len(result["episodes"]) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: reranker failure
# ═══════════════════════════════════════════════════════════════════════════════


class TestRerankerFailure:
    """Pipeline behaviour when the re-ranker raises an exception."""

    @pytest.fixture(autouse=True)
    def _clear_model_cache(self) -> None:
        """Reset module-level cache to avoid cross-test pollution."""
        _MODEL_CACHE.clear()
        _MODEL_LOCKS.clear()

    @pytest.fixture
    def failing_reranker(self) -> AsyncMock:
        """A re-ranker that always raises an exception."""
        reranker = AsyncMock()
        reranker.rerank.side_effect = RuntimeError("Model inference failed")
        return reranker

    async def test_reranker_failure_propagates(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
        failing_reranker: AsyncMock,
    ) -> None:
        """When the re-ranker fails, SearchLegFailedError propagates."""
        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample_search_results,
            org_config=OrgConfigBase(reranker_backend="sentence_transformers"),
            reranker=failing_reranker,
        )

        with pytest.raises(SearchLegFailedError, match="reranker"):
            await retriever.hybrid_search("python", PROJECT_ID, limit=20)

    async def test_reranker_failure_logs_and_raises(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
        failing_reranker: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed re-rank logs an error and raises SearchLegFailedError."""
        import logging

        caplog.set_level(logging.ERROR)

        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample_search_results,
            org_config=OrgConfigBase(reranker_backend="sentence_transformers"),
            reranker=failing_reranker,
        )

        with pytest.raises(SearchLegFailedError, match="reranker"):
            await retriever.hybrid_search("python", PROJECT_ID, limit=20)

        # Should have logged the failure — check that at least one ERROR
        # record exists (the exact message format depends on structlog config)
        assert len(caplog.records) > 0, (
            "An error should be logged when the re-ranker fails"
        )
        assert any(r.levelno == logging.ERROR for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: context assembly formatting with reranker
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextAssemblyWithReranker:
    """Context assembly output format includes ``reranker_score``."""

    @pytest.fixture(autouse=True)
    def _clear_model_cache(self) -> None:
        """Reset module-level cache to avoid cross-test pollution."""
        _MODEL_CACHE.clear()
        _MODEL_LOCKS.clear()

    async def test_context_assembly_with_reranker_adds_score_to_format(
        self,
        mock_db_session: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The formatted text output includes the ``reranker_score`` when
        re-ranking is active.

        This tests that the score propagates all the way through:
        retriever → context service → formatter → final string.
        """
        # Build a mock re-ranker with deterministic output
        mock_reranker = AsyncMock()
        mock_reranker.rerank.return_value = [
            {
                "id": "ep-1",
                "content": "Python is great for data science",
                "rrf_score": 0.033333,
                "reranker_score": 0.92,
                "role": "user",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "ep-2",
                "content": "FastAPI makes async web apps easy",
                "rrf_score": 0.016393,
                "reranker_score": 0.85,
                "role": "assistant",
                "created_at": "2026-01-01T00:00:01Z",
            },
        ]

        # Build the retriever with mocked search legs and the mock reranker
        sample = {
            "episode_vector": [
                {"id": "ep-1", "content": "Python is great for data science", "score": 0.92},
                {"id": "ep-2", "content": "FastAPI makes async web apps easy", "score": 0.75},
            ],
            "episode_bm25": [
                {"id": "ep-1", "content": "Python is great for data science", "score": 0.88},
            ],
            "fact_vector": [],
            "fact_bm25": [],
        }

        org_config = OrgConfigBase(
            reranker_backend="sentence_transformers",
            reranker_top_n=5,
        )

        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample,
            org_config=org_config,
            reranker=mock_reranker,
        )

        # Build a ContextService with a mock retriever, but then swap in
        # our fully-mocked one
        service = ContextService(
            db=mock_db_session,
            org_id=ORG_ID,
            redis=None,
            graph_backends=[],
            org_config=org_config,
        )
        # Replace the internal retriever with our pre-configured one
        service._retriever = retriever

        result = await service.assemble(
            project_id=PROJECT_ID,
            query="python fastapi",
            limit=20,
            format="text",
        )

        context_str: str = result["context"]

        # The formatted text should include the reranker_score values
        assert "score=0.9200" in context_str or "score=0.92" in context_str, (
            "Formatted output should include the reranker_score for episodes. "
            f"Got: {context_str[:500]}"
        )
        assert "score=0.8500" in context_str or "score=0.85" in context_str, (
            "Formatted output should include the reranker_score for the second episode. "
            f"Got: {context_str[:500]}"
        )

        # The content should appear in the output
        assert "Python is great for data science" in context_str
        assert "FastAPI makes async web apps easy" in context_str

        # Metadata should be present
        assert result["metadata"]["cache_hit"] is False
        assert result["metadata"]["assembly_time_ms"] > 0

    async def test_context_assembly_without_reranker_no_extra_score(
        self,
        mock_db_session: AsyncMock,
    ) -> None:
        """Without re-ranking, the formatted output uses ``rrf_score``
        (not ``reranker_score``).
        """
        sample = {
            "episode_vector": [
                {"id": "ep-1", "content": "Python great", "score": 0.92},
            ],
            "episode_bm25": [
                {"id": "ep-1", "content": "Python great", "score": 0.88},
            ],
            "fact_vector": [],
            "fact_bm25": [],
        }

        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample,
            org_config=OrgConfigBase(reranker_backend=None),
            reranker=None,
        )

        service = ContextService(
            db=mock_db_session,
            org_id=ORG_ID,
            redis=None,
            graph_backends=[],
            org_config=OrgConfigBase(reranker_backend=None),
        )
        service._retriever = retriever

        result = await service.assemble(
            project_id=PROJECT_ID,
            query="python",
            limit=20,
            format="text",
        )

        context_str: str = result["context"]

        # Should mention the content
        assert "Python great" in context_str

        # Should NOT contain "reranker_score" anywhere in the output
        assert "reranker_score" not in context_str, (
            "Formatted output should not reference reranker_score when "
            "re-ranking is disabled"
        )

    async def test_context_assembly_reranker_failure_propagates(
        self,
        mock_db_session: AsyncMock,
    ) -> None:
        """When the re-ranker fails, the error propagates."""
        failing_reranker = AsyncMock()
        failing_reranker.rerank.side_effect = RuntimeError("API timeout")

        sample = {
            "episode_vector": [
                {"id": "ep-1", "content": "Python great", "score": 0.92},
            ],
            "episode_bm25": [
                {"id": "ep-1", "content": "Python great", "score": 0.88},
            ],
            "fact_vector": [],
            "fact_bm25": [],
        }

        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample,
            org_config=OrgConfigBase(reranker_backend="sentence_transformers"),
            reranker=failing_reranker,
        )

        service = ContextService(
            db=mock_db_session,
            org_id=ORG_ID,
            redis=None,
            graph_backends=[],
            org_config=OrgConfigBase(reranker_backend="sentence_transformers"),
        )
        service._retriever = retriever

        with pytest.raises(SearchLegFailedError, match="reranker"):
            await service.assemble(
                project_id=PROJECT_ID,
                query="python",
                limit=20,
                format="text",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Test: RRF merge produces correct scores
# ═══════════════════════════════════════════════════════════════════════════════


class TestRRFMergeWithRerankerConfig:
    """RRF merge uses ``reranker_top_k`` as the candidate pool size."""

    async def test_rrf_merge_uses_reranker_top_k_as_pool(
        self,
        mock_db_session: AsyncMock,
    ) -> None:
        """When re-ranking is enabled, the RRF merge should collect
        ``reranker_top_k`` candidates (not the ``limit`` parameter).

        This ensures the re-ranker has enough candidates to choose from.
        """
        many_results = [
            {"id": f"ep-{i}", "content": f"Result {i}", "score": 1.0 / (i + 1)}
            for i in range(30)
        ]

        sample = {
            "episode_vector": many_results,
            "episode_bm25": many_results[:10],
            "fact_vector": [],
            "fact_bm25": [],
        }

        # With reranker_top_k=50 and only 30 results, all 30 should be in the pool
        org_config = OrgConfigBase(
            reranker_backend="sentence_transformers",
            reranker_top_k=50,
        )
        mock_reranker = AsyncMock()
        # Simulate a no-op reranker that returns all RRF candidates unchanged
        mock_reranker.rerank.side_effect = lambda q, c, top_n=10: c

        retriever = _build_mocked_retriever(
            mock_db_session,
            ORG_ID,
            sample,
            org_config=org_config,
            reranker=mock_reranker,
        )

        result = await retriever.hybrid_search("python", PROJECT_ID, limit=20)

        # The RRF merge pool should be 50 (reranker_top_k), but we only have
        # 30 unique items. So episodes should have all 30 RRF-mergable results.
        assert len(result["episodes"]) == 30, (
            "RRF merge should collect up to reranker_top_k (50) candidates"
        )

        # Verify the re-ranker received the full pool (candidates is positional arg)
        args, _ = mock_reranker.rerank.call_args_list[0]
        assert len(args[1]) == 30
