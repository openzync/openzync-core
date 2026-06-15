"""Tests for OZGraphRetriever."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langchain_core.documents import Document

from openzep.client import AsyncOpenZep
from openzep.integrations.langchain.retriever import OZGraphRetriever

SAMPLE_SEARCH_RESULTS = [
    {
        "content": "Alice works at Acme Corp as a software engineer.",
        "score": 0.95,
        "type": "fact",
        "node_name": "Alice",
        "node_id": "node-1",
    },
    {
        "content": "Alice mentioned she is working on the Graphiti project.",
        "score": 0.82,
        "type": "episode",
        "node_name": "Episode-42",
        "node_id": "node-2",
    },
    {
        "content": "Acme Corp is based in San Francisco.",
        "score": 0.45,
        "type": "fact",
        "node_name": "Acme Corp",
        "node_id": "node-3",
    },
]


@pytest.fixture
def mock_client():
    """Create a real AsyncOpenZep with mocked graph sub-client."""
    client = AsyncOpenZep(api_key="test", base_url="http://test")
    client.graph = AsyncMock()
    return client


class TestOZGraphRetriever:
    """Tests for OZGraphRetriever."""

    def test_init(self, mock_client):
        """Initialise with required args."""
        retriever = OZGraphRetriever(
            client=mock_client,
            user_id="user-1",
            types="episodes,facts",
            k=5,
        )
        assert retriever.user_id == "user-1"
        assert retriever.types == "episodes,facts"
        assert retriever.k == 5

    @pytest.mark.asyncio
    async def test_aget_relevant_documents(self, mock_client):
        """Returns documents from graph search."""
        mock_client.graph.search.return_value = SAMPLE_SEARCH_RESULTS

        retriever = OZGraphRetriever(
            client=mock_client,
            user_id="user-1",
            k=5,
        )
        docs = await retriever._aget_relevant_documents("Alice Acme Corp")

        assert len(docs) == 3
        assert isinstance(docs[0], Document)
        assert docs[0].page_content == SAMPLE_SEARCH_RESULTS[0]["content"]
        assert docs[0].metadata["score"] == 0.95
        assert docs[0].metadata["type"] == "fact"
        assert docs[0].metadata["source"] == "openzep_graph"

    @pytest.mark.asyncio
    async def test_score_threshold_filters(self, mock_client):
        """Results below score_threshold are excluded."""
        mock_client.graph.search.return_value = SAMPLE_SEARCH_RESULTS

        retriever = OZGraphRetriever(
            client=mock_client,
            user_id="user-1",
            score_threshold=0.8,
        )
        docs = await retriever._aget_relevant_documents("Alice")

        assert len(docs) == 2
        assert docs[0].metadata["score"] == 0.95
        assert docs[1].metadata["score"] == 0.82

    @pytest.mark.asyncio
    async def test_empty_results(self, mock_client):
        """Returns empty list when no results."""
        mock_client.graph.search.return_value = []

        retriever = OZGraphRetriever(
            client=mock_client,
            user_id="user-1",
        )
        docs = await retriever._aget_relevant_documents("nothing")
        assert docs == []

    @pytest.mark.asyncio
    async def test_passes_correct_params(self, mock_client):
        """Search is called with the right user_id, query, types, limit."""
        mock_client.graph.search.return_value = SAMPLE_SEARCH_RESULTS

        retriever = OZGraphRetriever(
            client=mock_client,
            user_id="user-1",
            types="facts",
            k=10,
        )
        await retriever._aget_relevant_documents("test query")

        mock_client.graph.search.assert_awaited_once_with(
            "user-1", "test query", types="facts", limit=10
        )


