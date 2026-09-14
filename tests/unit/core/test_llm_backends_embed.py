"""Unit tests for ``encoding_format="float"`` on OpenAI-compatible embeds."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_sdk_client(vector: list[float]) -> AsyncMock:
    """Build a fake SDK client whose ``embeddings.create`` returns one vector."""
    client = AsyncMock()
    item = MagicMock()
    item.embedding = vector
    response = MagicMock()
    response.data = [item]
    client.embeddings.create = AsyncMock(return_value=response)
    return client


@pytest.mark.unit
class TestEmbedEncodingFormat:
    """All OpenAI-compatible backends must request float vectors explicitly."""

    @pytest.mark.asyncio
    async def test_openai_embed_passes_float(self) -> None:
        """OpenAIBackend passes ``encoding_format="float"`` to the SDK."""
        from core.llm_backends import OpenAIBackend

        client = _mock_sdk_client([0.1, 0.2])
        with patch("openai.AsyncOpenAI", return_value=client):
            backend = OpenAIBackend(api_key="test-key")

        result = await backend.embed(["hello"], model="text-embedding-3-small")

        client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input=["hello"],
            encoding_format="float",
        )
        assert result.dim == 2

    @pytest.mark.asyncio
    async def test_azure_embed_passes_float(self) -> None:
        """AzureBackend passes ``encoding_format="float"`` to the SDK."""
        from core.llm_backends import AzureBackend

        client = _mock_sdk_client([0.1, 0.2, 0.3])
        with patch("openai.AsyncAzureOpenAI", return_value=client):
            backend = AzureBackend(
                endpoint="https://test.openai.azure.com",
                api_key="test-key",
                deployment="test-deployment",
            )

        result = await backend.embed(["hello"])

        client.embeddings.create.assert_called_once_with(
            model="test-deployment",
            input=["hello"],
            encoding_format="float",
        )
        assert result.dim == 3

    @pytest.mark.asyncio
    async def test_openai_like_embed_passes_float(self) -> None:
        """OpenAILikeBackend passes ``encoding_format="float"`` to the SDK."""
        from core.llm_backends import OpenAILikeBackend

        client = _mock_sdk_client([0.1, 0.2])
        with patch("openai.AsyncOpenAI", return_value=client):
            backend = OpenAILikeBackend(base_url="http://localhost:8000/v1")

        result = await backend.embed(["hello"], model="test-model")

        client.embeddings.create.assert_called_once_with(
            model="test-model",
            input=["hello"],
            encoding_format="float",
        )
        assert result.dim == 2
