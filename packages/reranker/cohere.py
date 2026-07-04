"""Cohere API re-ranker — remote API backend.

Implements ``CrossEncoderReranker`` using the Cohere Rerank API.
The Cohere client is created lazily on first use.

Usage::

    reranker = CohereReranker(api_key="...")
    reranked = await reranker.rerank("query", candidates, top_n=10)

Optional dependency: ``pip install openzync[cohere]``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from packages.reranker.interface import CrossEncoderReranker

logger = logging.getLogger(__name__)


class CohereReranker(CrossEncoderReranker):
    """Re-ranker using the Cohere Rerank API.

    The Cohere client is created lazily on first use via
    :meth:`_ensure_client`.

    Attributes:
        DEFAULT_MODEL: Default Cohere rerank model name.
    """

    DEFAULT_MODEL = "rerank-english-v3.0"

    def __init__(self, api_key: str, model_name: str | None = None) -> None:
        """Initialize the Cohere re-ranker.

        Args:
            api_key: Cohere API key for authentication.
            model_name: Cohere rerank model name. Defaults to
                ``rerank-english-v3.0``.
        """
        self._api_key = api_key
        self._model_name = model_name or self.DEFAULT_MODEL
        self._client: Any = None

    @property
    def backend_name(self) -> str:
        """Return the backend identifier for metrics labels."""
        return "cohere"

    async def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """Re-rank candidates using the Cohere Rerank API.

        Args:
            query: The search query string.
            candidates: A list of candidate dicts with at least ``id``
                and ``content``.
            top_n: Maximum number of results to return.

        Returns:
            Candidates sorted by ``reranker_score`` descending, truncated
            to ``top_n``.
        """
        if not candidates:
            return []

        self._ensure_client()

        documents = [c["content"] for c in candidates if c.get("content")]
        if not documents:
            for c in candidates:
                c["reranker_score"] = 0.0
            return candidates[:top_n]

        # The Cohere SDK is synchronous — run off the event loop
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.rerank(
                query=query,
                documents=documents,
                model=self._model_name,
                top_n=top_n,
            ),
        )

        # Map Cohere's results back to candidates by index
        index_scores: dict[int, float] = {
            r.index: r.relevance_score for r in response.results
        }

        scored: list[dict[str, Any]] = []
        for i, c in enumerate(candidates):
            c["reranker_score"] = round(float(index_scores.get(i, 0.0)), 6)
            scored.append(c)

        scored.sort(key=lambda x: x.get("reranker_score", 0.0), reverse=True)
        return scored[:top_n]

    def _ensure_client(self) -> None:
        """Lazily initialise the Cohere client.

        Raises:
            ImportError: If the ``cohere`` package is not installed.
        """
        if self._client is not None:
            return

        try:
            import cohere  # noqa: PLC0415
        except ImportError as err:
            raise ImportError(
                "cohere is not installed. Install with: pip install openzync[cohere]"
            ) from err

        self._client = cohere.Client(
            api_key=self._api_key,
            timeout=30,
            max_retries=2,
        )
