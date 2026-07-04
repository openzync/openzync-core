"""LangChain retriever backed by OpenZync's knowledge graph search.

Provides ``OZGraphRetriever``, a ``BaseRetriever`` implementation that
uses OpenZync's hybrid graph search to surface relevant context from
past episodes, facts, and entities.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from openzync.client import AsyncOpenZync


class OZGraphRetriever(BaseRetriever):
    """Retriever that searches OpenZync's knowledge graph.

    Uses OpenZync's hybrid search (semantic + keyword + graph traversal)
    to retrieve contextually relevant documents from a project's memory.

    .. code-block:: python

        from openzync import AsyncOpenZync
        from openzync.integrations.langchain.retriever import OZGraphRetriever

        client = AsyncOpenZync(api_key="...")
        retriever = OZGraphRetriever(
            client=client,
            project_id="project-abc",
            types="episodes,facts",
            k=5,
        )
        docs = retriever.invoke("What does Alice know about Acme Corp?")

    Args:
        client: An ``AsyncOpenZync`` client instance.
        project_id: OpenZync project UUID to search within.
        types: Comma-separated result types to include
            (``"episodes"``, ``"facts"``, ``"entities"``).
            Defaults to ``"episodes,facts"``.
        k: Maximum number of results to return (default 5).
        score_threshold: Minimum relevance score (0-1) for results.
            ``None`` means no threshold.
    """

    client: AsyncOpenZync
    project_id: str
    types: str = "episodes,facts"
    k: int = 5
    score_threshold: float | None = None

    # ── Sync ────────────────────────────────────────────────────────────

    def _get_relevant_documents(
        self,
        query: str,
        **kwargs: Any,
    ) -> list[Document]:
        """Retrieve documents relevant to the query (sync).

        Args:
            query: Natural-language search query.
            **kwargs: Additional search parameters.

        Returns:
            List of ``Document`` objects with content and metadata.
        """
        return _run_async(self._aget_relevant_documents(query, **kwargs))

    # ── Async ───────────────────────────────────────────────────────────

    async def _aget_relevant_documents(
        self,
        query: str,
        **kwargs: Any,
    ) -> list[Document]:
        """Retrieve documents relevant to the query (async).

        Args:
            query: Natural-language search query.
            **kwargs: Additional search parameters.

        Returns:
            List of ``Document`` objects with content and metadata.
        """
        limit = kwargs.get("limit", self.k)
        results = await self.client.graph.search(
            self.project_id,
            query,
            types=kwargs.get("types", self.types),
            limit=limit,
        )

        documents: list[Document] = []
        for result in results:
            # Apply score threshold filtering
            score = result.get("score", 0.0)
            if self.score_threshold is not None and score < self.score_threshold:
                continue

            content = result.get("content", "") or result.get("name", "")
            metadata: dict[str, Any] = {
                "source": "openzync_graph",
                "project_id": self.project_id,
                "score": score,
                "type": result.get("type"),
                "node_name": result.get("node_name"),
                "node_id": result.get("node_id"),
            }
            documents.append(Document(page_content=content, metadata=metadata))

        return documents


def _run_async(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)
