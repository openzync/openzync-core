"""Unit tests for the cross-encoder re-ranker package.

Tests cover all classes in ``packages/reranker/``:

- ``CrossEncoderReranker`` (abstract interface contract)
- ``SentenceTransformersReranker`` (local model wrapper)
- ``CohereReranker`` (remote Cohere API wrapper)
- ``RerankerFactory`` (config-driven construction)

No real model loading or external API calls — all mocking via
``unittest.mock``.
"""

from __future__ import annotations

import builtins
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("numpy", reason="reranker tests require numpy")
import numpy as np

from packages.reranker import (
    CohereReranker,
    CrossEncoderReranker,
    RerankerFactory,
    SentenceTransformersReranker,
)
from packages.reranker.sentence_transformers import _MODEL_CACHE, _MODEL_LOCKS
from schemas.organization_config import OrgConfigBase

# ═══════════════════════════════════════════════════════════════════════════════
# Shared test data
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def sample_candidates() -> list[dict[str, Any]]:
    return [
        {"id": "1", "content": "Python is a programming language", "rrf_score": 0.8},
        {"id": "2", "content": "JavaScript runs in the browser", "rrf_score": 0.6},
        {"id": "3", "content": "FastAPI is a Python web framework", "rrf_score": 0.4},
    ]


@pytest.fixture
def mock_model() -> MagicMock:
    model = MagicMock()
    model.predict.return_value = np.array([0.9, 0.1, 0.5])
    return model


# ═══════════════════════════════════════════════════════════════════════════════
# CrossEncoderReranker — abstract interface contract
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestCrossEncoderReranker:
    """Verify the abstract interface contract."""

    def test_cannot_instantiate_abstract_class(self) -> None:
        """CrossEncoderReranker is abstract — instantiation must fail."""
        with pytest.raises(TypeError):
            CrossEncoderReranker()  # type: ignore[abstract]

    def test_rerank_is_abstract(self) -> None:
        """rerank() must be declared as an abstract method."""
        assert "rerank" in CrossEncoderReranker.__abstractmethods__


# ═══════════════════════════════════════════════════════════════════════════════
# SentenceTransformersReranker
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestSentenceTransformersReranker:
    """Local cross-encoder re-ranker tests."""

    # ── Fixtures ──────────────────────────────────────────────────────────

    @pytest.fixture(autouse=True)
    def _clear_model_cache(self) -> None:
        """Reset module-level model cache before each test.

        This prevents cross-test pollution since ``_MODEL_CACHE`` is a
        module-level global that persists across tests in the same session.
        """
        _MODEL_CACHE.clear()
        _MODEL_LOCKS.clear()

    # ── Happy path ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_rerank_returns_sorted_results(
        self,
        sample_candidates: list[dict[str, Any]],
        mock_model: MagicMock,
    ) -> None:
        """Results are sorted by ``reranker_score`` descending."""
        reranker = SentenceTransformersReranker()
        reranker._model = mock_model  # bypass lazy loading

        results = await reranker.rerank("python", sample_candidates, top_n=3)

        # Scores: 0.9 (id=1), 0.1 (id=2), 0.5 (id=3)
        # Expected order: id=1 (0.9), id=3 (0.5), id=2 (0.1)
        assert len(results) == 3
        assert results[0]["id"] == "1"
        assert results[1]["id"] == "3"
        assert results[2]["id"] == "2"
        assert results[0]["reranker_score"] == 0.9
        assert results[1]["reranker_score"] == 0.5
        assert results[2]["reranker_score"] == 0.1

    @pytest.mark.asyncio
    async def test_rerank_preserves_rrf_score(
        self,
        sample_candidates: list[dict[str, Any]],
        mock_model: MagicMock,
    ) -> None:
        """Original ``rrf_score`` is preserved in output dicts."""
        reranker = SentenceTransformersReranker()
        reranker._model = mock_model

        results = await reranker.rerank("python", sample_candidates, top_n=3)

        for result in results:
            assert "rrf_score" in result
            # rrf_score should be the original value from the candidate
            assert result["rrf_score"] in {0.8, 0.6, 0.4}
        assert results[0]["rrf_score"] == 0.8  # Python entry kept its rrf_score

    @pytest.mark.asyncio
    async def test_rerank_adds_reranker_score(
        self,
        sample_candidates: list[dict[str, Any]],
        mock_model: MagicMock,
    ) -> None:
        """Every result gets a ``reranker_score`` key."""
        reranker = SentenceTransformersReranker()
        reranker._model = mock_model

        results = await reranker.rerank("python", sample_candidates, top_n=3)

        for result in results:
            assert "reranker_score" in result
            assert isinstance(result["reranker_score"], float)

    # ── Edge cases ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_rerank_empty_candidates(
        self,
        mock_model: MagicMock,
    ) -> None:
        """Empty candidate list returns an empty list."""
        reranker = SentenceTransformersReranker()
        reranker._model = mock_model

        results = await reranker.rerank("python", [], top_n=10)

        assert results == []

    @pytest.mark.asyncio
    async def test_rerank_no_content_field(self) -> None:
        """Candidates without a ``content`` field get a score of 0.0."""
        reranker = SentenceTransformersReranker()
        # Model should not be called — no content to pair
        reranker._model = MagicMock()

        candidates = [
            {"id": "1", "rrf_score": 0.8},
            {"id": "2", "rrf_score": 0.6},
        ]
        results = await reranker.rerank("python", candidates, top_n=5)

        assert len(results) == 2
        for result in results:
            assert result["reranker_score"] == 0.0
        # predict() must NOT have been called — no content to infer on
        reranker._model.predict.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerank_top_n_respected(
        self,
        sample_candidates: list[dict[str, Any]],
        mock_model: MagicMock,
    ) -> None:
        """With top_n=2 only 2 results are returned."""
        reranker = SentenceTransformersReranker()
        reranker._model = mock_model

        results = await reranker.rerank("python", sample_candidates, top_n=2)

        assert len(results) == 2
        # top results should be id=1 (0.9) and id=3 (0.5)
        assert results[0]["id"] == "1"
        assert results[1]["id"] == "3"

    @pytest.mark.asyncio
    async def test_rerank_candidates_without_content_handled(
        self,
        mock_model: MagicMock,
    ) -> None:
        """Mix of content/no-content candidates — no-content get 0.0.

        Model scores: [0.9, 0.5] for the two candidates with content.
        The no-content candidate should get 0.0 and end up last.
        """
        reranker = SentenceTransformersReranker()
        # We need to control the model output for only the content-bearing pairs
        model = MagicMock()
        model.predict.return_value = np.array([0.9, 0.5])
        reranker._model = model

        candidates = [
            {"id": "1", "content": "Python is great", "rrf_score": 0.8},
            {"id": "2", "rrf_score": 0.6},  # no content
            {"id": "3", "content": "FastAPI is async", "rrf_score": 0.4},
        ]
        results = await reranker.rerank("python", candidates, top_n=3)

        # id=1 (0.9) > id=3 (0.5) > id=2 (0.0)
        assert len(results) == 3
        assert results[0]["id"] == "1"
        assert results[0]["reranker_score"] == 0.9
        assert results[1]["id"] == "3"
        assert results[1]["reranker_score"] == 0.5
        assert results[2]["id"] == "2"
        assert results[2]["reranker_score"] == 0.0

    # ── Model cache behaviour ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_model_cache_shared_across_instances(self) -> None:
        """Two instances with the same model name share the cached model.

        The ``_MODEL_CACHE`` should prevent loading the model a second time.
        Verify by checking both instances reference the same model object
        and the cache has exactly one entry.
        """
        mock_model_instance = MagicMock()
        mock_model_instance.predict.return_value = np.array([0.5])

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=mock_model_instance)

        with (
            patch.dict("sys.modules", {"sentence_transformers": MagicMock()}),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            r1 = SentenceTransformersReranker()
            r2 = SentenceTransformersReranker()

            await r1.rerank("q", [{"id": "1", "content": "test"}], top_n=1)
            await r2.rerank("q", [{"id": "2", "content": "test"}], top_n=1)

        # The cache should have exactly one entry
        assert len(_MODEL_CACHE) == 1, (
            "Expected exactly one model in the cache"
        )
        # Both instances reference the same model object
        assert r1._model is r2._model, (
            "Both instances should share the same cached model"
        )
        # run_in_executor is called for both model loading AND model.predict(),
        # so call_count is not a reliable check. Instead verify the model
        # was loaded only once by checking cache entry count.
        assert _MODEL_CACHE[
            SentenceTransformersReranker.DEFAULT_MODEL
        ] is mock_model_instance, (
            "Cached model should be the one returned by run_in_executor"
        )

    @pytest.mark.asyncio
    async def test_model_load_executor_used(self) -> None:
        """Model loading via ``run_in_executor`` is called when cache is cold.

        Verify that the first ``run_in_executor`` call loads the model
        (CrossEncoder constructor), not just inference.
        """
        mock_model_instance = MagicMock()
        mock_model_instance.predict.return_value = np.array([0.5])

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=mock_model_instance)

        with (
            patch.dict("sys.modules", {"sentence_transformers": MagicMock()}),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            reranker = SentenceTransformersReranker()
            await reranker.rerank("q", [{"id": "1", "content": "test"}], top_n=1)

        # First call should be model loading; inference is the second call
        first_call = mock_loop.run_in_executor.await_args_list[0]
        args, _ = first_call
        assert args[0] is None, (
            "First argument should be None (default executor)"
        )
        # The callable should be loading a model (lambda wrapping CrossEncoder)
        assert callable(args[1]), (
            "Expected a callable for model loading, "
            f"got {type(args[1]).__name__}"
        )
        # Verify the second call is also run_in_executor (for predict call)
        assert mock_loop.run_in_executor.await_count >= 2, (
            "Expected at least 2 run_in_executor calls (model load + predict)"
        )

    # ── Import error ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_import_error_raised_when_not_installed(self) -> None:
        """Without ``sentence-transformers``, a clear ``ImportError`` is raised."""
        orig_import = builtins.__import__

        def mock_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "sentence_transformers":
                msg = "No module named 'sentence_transformers'"
                raise ImportError(msg)
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            reranker = SentenceTransformersReranker()
            with pytest.raises(
                ImportError,
                match="sentence-transformers is not installed",
            ):
                await reranker.rerank(
                    "q",
                    [{"id": "1", "content": "test"}],
                    top_n=1,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# CohereReranker
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestCohereReranker:
    """Remote Cohere API re-ranker tests."""

    # ── Fixtures ──────────────────────────────────────────────────────────

    @pytest.fixture
    def mock_cohere_client(self) -> MagicMock:
        """Simulate ``cohere.Client`` for API mocking."""
        return MagicMock()

    @pytest.fixture
    def mock_cohere_response(self) -> MagicMock:
        """Simulate ``cohere.Client.rerank()`` response.

        Returns results with indices reversed relative to input order:
        - Index 2 (FastAPI) → 0.95
        - Index 0 (Python)  → 0.80
        - Index 1 (JS)      → 0.30
        """
        response = MagicMock()
        result_1 = MagicMock(index=2, relevance_score=0.95)
        result_2 = MagicMock(index=0, relevance_score=0.80)
        result_3 = MagicMock(index=1, relevance_score=0.30)
        response.results = [result_1, result_2, result_3]
        return response

    @pytest.fixture
    def reranker(self) -> CohereReranker:
        """CohereReranker instance with pre-set API key.

        The ``_client`` is NOT set here — individual tests set it via
        ``reranker._client = mock_cohere_client`` to control per-test
        behaviour.
        """
        return CohereReranker(api_key="test-key")

    # ── Happy path ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_rerank_reorders_by_cohere_score(
        self,
        reranker: CohereReranker,
        mock_cohere_client: MagicMock,
        mock_cohere_response: MagicMock,
        sample_candidates: list[dict[str, Any]],
    ) -> None:
        """Results are re-ordered by Cohere's relevance_score.

        Cohere returns: index=2 (0.95), index=0 (0.80), index=1 (0.30).
        Expected output order: id=3 (0.95), id=1 (0.80), id=2 (0.30).
        """
        mock_cohere_client.rerank.return_value = mock_cohere_response
        reranker._client = mock_cohere_client

        results = await reranker.rerank("python", sample_candidates, top_n=3)

        assert len(results) == 3
        assert results[0]["id"] == "3", "FastAPI (index 2, score 0.95) should rank first"
        assert results[1]["id"] == "1", "Python (index 0, score 0.80) should rank second"
        assert results[2]["id"] == "2", "JS (index 1, score 0.30) should rank third"

        assert results[0]["reranker_score"] == 0.95
        assert results[1]["reranker_score"] == 0.80
        assert results[2]["reranker_score"] == 0.30

    @pytest.mark.asyncio
    async def test_rerank_preserves_rrf_score(
        self,
        reranker: CohereReranker,
        mock_cohere_client: MagicMock,
        mock_cohere_response: MagicMock,
        sample_candidates: list[dict[str, Any]],
    ) -> None:
        """Original ``rrf_score`` is preserved after Cohere re-ranking."""
        mock_cohere_client.rerank.return_value = mock_cohere_response
        reranker._client = mock_cohere_client

        results = await reranker.rerank("python", sample_candidates, top_n=3)

        for result in results:
            assert "rrf_score" in result

    # ── Edge cases ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_rerank_empty_candidates(
        self,
        reranker: CohereReranker,
        mock_cohere_client: MagicMock,
    ) -> None:
        """Empty candidate list returns an empty list."""
        reranker._client = mock_cohere_client

        results = await reranker.rerank("python", [], top_n=10)

        assert results == []
        mock_cohere_client.rerank.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerank_no_content_field(
        self,
        reranker: CohereReranker,
        mock_cohere_client: MagicMock,
    ) -> None:
        """Candidates without ``content`` get 0.0 score and API is not called."""
        reranker._client = mock_cohere_client

        candidates = [
            {"id": "1", "rrf_score": 0.8},
            {"id": "2", "rrf_score": 0.6},
        ]
        results = await reranker.rerank("python", candidates, top_n=5)

        assert len(results) == 2
        for result in results:
            assert result["reranker_score"] == 0.0
        # API must NOT be called — nothing to re-rank
        mock_cohere_client.rerank.assert_not_called()

    # ── Error handling ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_api_error_propagates(
        self,
        mock_cohere_client: MagicMock,
        sample_candidates: list[dict[str, Any]],
    ) -> None:
        """Cohere API errors propagate — fallback is at ``hybrid_search()``."""
        mock_cohere_client.rerank.side_effect = Exception("API timeout")
        reranker = CohereReranker(api_key="test-key")
        reranker._client = mock_cohere_client

        with pytest.raises(Exception, match="API timeout"):
            await reranker.rerank("query", sample_candidates, top_n=3)

    # ── Import error ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_import_error_raised_when_not_installed(
        self,
    ) -> None:
        """Without ``cohere`` package, a clear ``ImportError`` is raised."""
        reranker = CohereReranker(api_key="test-key")

        # _client is None, so _ensure_client will try to import cohere
        orig_import = builtins.__import__

        def mock_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "cohere":
                msg = "No module named 'cohere'"
                raise ImportError(msg)
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(
                ImportError,
                match="cohere is not installed",
            ):
                await reranker.rerank(
                    "q",
                    [{"id": "1", "content": "test"}],
                    top_n=1,
                )

    @pytest.mark.asyncio
    async def test_missing_api_key(
        self,
        sample_candidates: list[dict[str, Any]],
    ) -> None:
        """CohereReranker is creatable; the factory is responsible for None."""
        # This is a valid object — it only fails at _ensure_client time
        reranker = CohereReranker(api_key="")
        assert reranker._api_key == ""


# ═══════════════════════════════════════════════════════════════════════════════
# RerankerFactory
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestRerankerFactory:
    """Config-driven factory tests."""

    # ── Fixtures ──────────────────────────────────────────────────────────

    @pytest.fixture
    def org_config_with_sentence_transformers(self) -> OrgConfigBase:
        return OrgConfigBase(reranker_backend="sentence_transformers")

    @pytest.fixture
    def org_config_with_cohere(self) -> OrgConfigBase:
        return OrgConfigBase(reranker_backend="cohere", cohere_api_key="test-key")

    @pytest.fixture
    def org_config_disabled(self) -> OrgConfigBase:
        return OrgConfigBase(reranker_backend=None)

    # ── Disabled / unknown backends ───────────────────────────────────────

    def test_backend_null_returns_none(self, org_config_disabled: OrgConfigBase) -> None:
        """``reranker_backend=None`` returns ``None``."""
        assert RerankerFactory.create(org_config_disabled) is None

    def test_backend_empty_string_returns_none(self) -> None:
        """``reranker_backend=""`` returns ``None``."""
        config = OrgConfigBase(reranker_backend="")
        assert RerankerFactory.create(config) is None

    def test_unknown_backend_returns_none(self) -> None:
        """An unsupported backend string returns ``None``."""
        config = OrgConfigBase(reranker_backend="some_unknown_backend")
        assert RerankerFactory.create(config) is None

    # ── SentenceTransformers ──────────────────────────────────────────────

    def test_sentence_transformers_returns_instance(
        self,
        org_config_with_sentence_transformers: OrgConfigBase,
    ) -> None:
        """With the correct backend and import available, an instance is returned."""
        with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
            reranker = RerankerFactory.create(org_config_with_sentence_transformers)

        assert isinstance(reranker, SentenceTransformersReranker)

    def test_sentence_transformers_default_model_name(
        self,
        org_config_with_sentence_transformers: OrgConfigBase,
    ) -> None:
        """When no ``reranker_model`` is set, the default model is used."""
        with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
            reranker = RerankerFactory.create(org_config_with_sentence_transformers)

        assert reranker is not None
        assert reranker._model_name == SentenceTransformersReranker.DEFAULT_MODEL

    def test_sentence_transformers_custom_model_name(self) -> None:
        """A custom ``reranker_model`` is passed through to the instance."""
        config = OrgConfigBase(
            reranker_backend="sentence_transformers",
            reranker_model="cross-encoder/custom-model",
        )
        with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
            reranker = RerankerFactory.create(config)

        assert reranker is not None
        assert reranker._model_name == "cross-encoder/custom-model"

    def test_sentence_transformers_import_error_returns_none(
        self,
        org_config_with_sentence_transformers: OrgConfigBase,
    ) -> None:
        """When ``sentence-transformers`` is not installed, returns ``None``."""
        orig_import = builtins.__import__

        def mock_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "sentence_transformers":
                msg = "No module named 'sentence_transformers'"
                raise ImportError(msg)
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            reranker = RerankerFactory.create(org_config_with_sentence_transformers)

        assert reranker is None

    # ── Cohere ────────────────────────────────────────────────────────────

    def test_cohere_returns_instance(
        self,
        org_config_with_cohere: OrgConfigBase,
    ) -> None:
        """With API key and import available, a ``CohereReranker`` is returned."""
        with patch.dict("sys.modules", {"cohere": MagicMock()}):
            reranker = RerankerFactory.create(org_config_with_cohere)

        assert isinstance(reranker, CohereReranker)
        assert reranker is not None
        assert reranker._api_key == "test-key"

    def test_cohere_custom_model_name(self) -> None:
        """A custom ``reranker_model`` is passed through to Cohere."""
        config = OrgConfigBase(
            reranker_backend="cohere",
            cohere_api_key="test-key",
            reranker_model="rerank-english-v2.0",
        )
        with patch.dict("sys.modules", {"cohere": MagicMock()}):
            reranker = RerankerFactory.create(config)

        assert reranker is not None
        assert reranker._model_name == "rerank-english-v2.0"

    def test_cohere_no_api_key_returns_none(self) -> None:
        """Without a Cohere API key, the factory returns ``None``."""
        config = OrgConfigBase(reranker_backend="cohere", cohere_api_key=None)
        reranker = RerankerFactory.create(config)
        assert reranker is None

    def test_cohere_empty_api_key_returns_none(self) -> None:
        """With an empty Cohere API key, the factory returns ``None``."""
        config = OrgConfigBase(reranker_backend="cohere", cohere_api_key="")
        reranker = RerankerFactory.create(config)
        assert reranker is None

    def test_cohere_import_error_returns_none(
        self,
        org_config_with_cohere: OrgConfigBase,
    ) -> None:
        """When ``cohere`` package is not installed, returns ``None``."""
        orig_import = builtins.__import__

        def mock_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "cohere":
                msg = "No module named 'cohere'"
                raise ImportError(msg)
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            reranker = RerankerFactory.create(org_config_with_cohere)

        assert reranker is None
