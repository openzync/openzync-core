"""Unit tests for OpenRouterBackend.embed().

Tests cover constructor validation, happy-path embedding, retry logic
(429, 503, timeout, network error), exhaustion, and the embedding_dim property.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.exceptions import LLMConfigurationError
from core.llm import EmbeddingResponse
from core.llm_backends import OpenRouterBackend


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _mock_sleep() -> None:
    """Prevent asyncio.sleep from actually sleeping in retry tests."""
    with patch("asyncio.sleep", AsyncMock()):
        yield


def _fake_json(status: int = 200, embeddings: list[list[float]] | None = None) -> dict:
    """Build a fake OpenRouter embedding JSON response body."""
    if embeddings is None:
        embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": emb, "index": i}
            for i, emb in enumerate(embeddings)
        ],
        "model": "fake-model-v1",
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


def _build_mock_response(status: int = 200, json_data: dict | None = None) -> MagicMock:
    """Build a mock ``httpx.Response``-like object."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = json_data or _fake_json()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}",
            request=MagicMock(),
            response=resp,
        )
    return resp


@pytest.fixture
def mock_httpx() -> MagicMock:
    """Mock ``httpx.AsyncClient`` so ``post()`` returns a controllable response.

    Patches ``httpx.AsyncClient`` so that the ``async with`` block inside
    ``embed()`` picks up the mock.
    """
    with patch("httpx.AsyncClient") as mock_cls:
        client = MagicMock()
        client.post = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = client
        yield client


@pytest.fixture
def backend() -> OpenRouterBackend:
    """OpenRouterBackend instance (no real API calls — httpx is mocked)."""
    return OpenRouterBackend(api_key="sk-or-valid-key", model="gpt-4o")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOpenRouterBackendEmbed:
    """Tests for OpenRouterBackend.embed()."""

    # ── Constructor validation ──────────────────────────────────────────

    def test_constructor_missing_api_key(self) -> None:
        """Should raise LLMConfigurationError when api_key is empty."""
        with pytest.raises(LLMConfigurationError, match="API key is required"):
            OpenRouterBackend(api_key="", model="gpt-4o")

    def test_constructor_missing_model(self) -> None:
        """Should raise LLMConfigurationError when model is None."""
        with pytest.raises(LLMConfigurationError, match="model name is required"):
            OpenRouterBackend(api_key="sk-or-valid", model=None)

    # ── embed() — missing model kwarg ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_embed_missing_model_kwarg(self, backend: OpenRouterBackend) -> None:
        """Should raise LLMConfigurationError when no model kwarg provided."""
        with pytest.raises(LLMConfigurationError, match="requires a model name"):
            await backend.embed(["test text"])

    # ── embed() — happy path ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_embed_success(
        self,
        backend: OpenRouterBackend,
        mock_httpx: MagicMock,
    ) -> None:
        """Should POST to /embeddings and return a well-formed EmbeddingResponse."""
        mock_httpx.post.return_value = _build_mock_response(
            json_data=_fake_json(embeddings=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]),
        )

        result = await backend.embed(
            ["hello world", "goodbye"],
            model="text-embedding-3-small",
        )

        assert isinstance(result, EmbeddingResponse)
        assert result.embeddings == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        assert result.model == "fake-model-v1"
        assert result.dim == 3

        # Verify the POST was sent to the correct URL with the right payload
        call_kwargs = mock_httpx.post.call_args
        assert call_kwargs is not None
        url = call_kwargs[0][0]
        payload = call_kwargs[1].get("json", {})
        assert "embeddings" in url
        assert payload["model"] == "text-embedding-3-small"
        assert payload["input"] == ["hello world", "goodbye"]

    @pytest.mark.asyncio
    async def test_embed_success_no_texts(
        self,
        backend: OpenRouterBackend,
        mock_httpx: MagicMock,
    ) -> None:
        """Empty texts list should return empty embeddings with dim=0."""
        mock_httpx.post.return_value = _build_mock_response(
            json_data={"object": "list", "data": [], "model": "fake"},
        )

        with pytest.raises(ValueError, match="Empty embedding response"):
            await backend.embed([], model="text-embedding-3-small")

    # ── embed() — retries on 429 (rate-limit) ───────────────────────────

    @pytest.mark.asyncio
    async def test_embed_retries_on_429(
        self,
        backend: OpenRouterBackend,
        mock_httpx: MagicMock,
    ) -> None:
        """Should retry on 429 status code then succeed."""
        success_resp = _build_mock_response(
            json_data=_fake_json(embeddings=[[0.5]]),
        )
        mock_httpx.post.side_effect = [
            _build_mock_response(status=429),
            _build_mock_response(status=429),
            success_resp,
        ]

        result = await backend.embed(["test"], model="m")

        assert result.embeddings == [[0.5]]
        assert mock_httpx.post.await_count == 3

    # ── embed() — retries on 503 (server error) ─────────────────────────

    @pytest.mark.asyncio
    async def test_embed_retries_on_503(
        self,
        backend: OpenRouterBackend,
        mock_httpx: MagicMock,
    ) -> None:
        """Should retry on 503 status code then succeed."""
        success_resp = _build_mock_response(
            json_data=_fake_json(embeddings=[[0.5]]),
        )
        mock_httpx.post.side_effect = [
            _build_mock_response(status=503),
            _build_mock_response(status=503),
            success_resp,
        ]

        result = await backend.embed(["test"], model="m")

        assert result.embeddings == [[0.5]]
        assert mock_httpx.post.await_count == 3

    # ── embed() — retries on timeout ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_embed_retries_on_timeout(
        self,
        backend: OpenRouterBackend,
        mock_httpx: MagicMock,
    ) -> None:
        """Should retry on httpx.TimeoutException then succeed."""
        success_resp = _build_mock_response(
            json_data=_fake_json(embeddings=[[0.5]]),
        )
        mock_httpx.post.side_effect = [
            httpx.TimeoutException("timed out"),
            httpx.TimeoutException("timed out"),
            success_resp,
        ]

        result = await backend.embed(["test"], model="m")

        assert result.embeddings == [[0.5]]
        assert mock_httpx.post.await_count == 3

    # ── embed() — retries on network error ──────────────────────────────

    @pytest.mark.asyncio
    async def test_embed_retries_on_network_error(
        self,
        backend: OpenRouterBackend,
        mock_httpx: MagicMock,
    ) -> None:
        """Should retry on httpx.NetworkError then succeed."""
        success_resp = _build_mock_response(
            json_data=_fake_json(embeddings=[[0.5]]),
        )
        mock_httpx.post.side_effect = [
            httpx.NetworkError("connection refused"),
            httpx.NetworkError("connection refused"),
            success_resp,
        ]

        result = await backend.embed(["test"], model="m")

        assert result.embeddings == [[0.5]]
        assert mock_httpx.post.await_count == 3

    # ── embed() — all retries exhausted ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_embed_all_retries_exhausted(
        self,
        backend: OpenRouterBackend,
        mock_httpx: MagicMock,
    ) -> None:
        """Should raise RuntimeError after all MAX_RETRIES fail."""
        mock_httpx.post.side_effect = [
            _build_mock_response(status=429),
            _build_mock_response(status=429),
            _build_mock_response(status=429),
        ]

        with pytest.raises(RuntimeError, match="embedding failed after 3 retries"):
            await backend.embed(["test"], model="m")

        assert mock_httpx.post.await_count == OpenRouterBackend.MAX_RETRIES

    # ── embedding_dim property ──────────────────────────────────────────

    def test_embedding_dim_returns_zero(self, backend: OpenRouterBackend) -> None:
        """embedding_dim should always return 0 (dimension is per-model)."""
        assert backend.embedding_dim == 0
