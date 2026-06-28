"""Abstract interface for cross-encoder re-rankers.

The ``CrossEncoderReranker`` ABC defines the contract every re-ranker
backend must satisfy.  Concrete implementations:

- :class:`SentenceTransformersReranker` — local ``sentence-transformers``
  cross-encoder model.
- :class:`CohereReranker` — Cohere Rerank API.

Usage::

    reranker: CrossEncoderReranker = SentenceTransformersReranker()
    reranked = await reranker.rerank("query text", candidates, top_n=10)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────────

RRF_K: int = 60
"""RRF constant — controls how quickly rank contribution decays."""

DEFAULT_RERANK_TOP_K: int = 50
"""Default number of RRF candidates to pass to the re-ranker."""

DEFAULT_RERANK_TOP_N: int = 10
"""Default number of results to return after re-ranking."""


# ── Abstract interface ─────────────────────────────────────────────────────


class CrossEncoderReranker(ABC):
    """Abstract re-ranker that scores and re-orders candidate documents.

    Subclasses implement :meth:`rerank` for a specific model backend
    (local cross-encoder or remote API).
    """

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Human-readable backend identifier for metrics and logging.

        Returns:
            A string like ``"sentence_transformers"`` or ``"cohere"``.
        """

    @abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """Re-rank candidate documents by relevance to the query.

        Args:
            query: The search query string.
            candidates: A list of candidate dicts.  Each must have at
                minimum an ``id`` and ``content`` key.  Items without
                ``content`` receive a ``reranker_score`` of ``0.0``.
            top_n: Maximum number of results to return.

        Returns:
            The same dicts with a ``reranker_score: float`` key added,
            sorted by ``reranker_score`` descending, truncated to
            ``top_n``.  The original ``rrf_score`` (if present) from
            the RRF merge is preserved.
        """
