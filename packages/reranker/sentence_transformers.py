"""SentenceTransformers cross-encoder re-ranker — local model backend.

Implements ``CrossEncoderReranker`` using a local
``sentence-transformers`` cross-encoder model.  The model is loaded
lazily on first use via a module-level cache with double-checked
locking to prevent concurrent loading of the same model.

Usage::

    reranker = SentenceTransformersReranker()
    reranked = await reranker.rerank("query", candidates, top_n=10)

Optional dependency: ``pip install openzync[reranker]``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from packages.reranker.interface import CrossEncoderReranker

logger = logging.getLogger(__name__)

# ── Module-level model cache (double-checked locking) ──────────────────────

_MODEL_CACHE: dict[str, Any] = {}
"""Lazy-loaded cross-encoder model instances, keyed by model name."""

_MODEL_LOCKS: dict[str, asyncio.Lock] = {}
"""Per-model-name asyncio locks to prevent concurrent model loading."""


# ── Implementation ─────────────────────────────────────────────────────────


class SentenceTransformersReranker(CrossEncoderReranker):
    """Re-ranker using a local ``sentence-transformers`` cross-encoder model.

    The model is loaded lazily on the first :meth:`rerank` call (not in
    ``__init__``) via a module-level cache with double-checked locking to
    prevent concurrent loading of the same model.

    Attributes:
        DEFAULT_MODEL: Default cross-encoder model name.
    """

    DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __init__(self, model_name: str | None = None) -> None:
        """Initialize the re-ranker.

        Args:
            model_name: HuggingFace model name for the cross-encoder.
                Defaults to ``cross-encoder/ms-marco-MiniLM-L-6-v2``.
        """
        self._model_name = model_name or self.DEFAULT_MODEL
        self._model: Any = None

    @property
    def backend_name(self) -> str:
        """Return the backend identifier for metrics labels."""
        return "sentence_transformers"

    async def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """Re-rank candidates using a local cross-encoder model.

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

        model = await self._ensure_model()

        # Build (query, content) pairs only for candidates that have content
        pairs = [(query, c["content"]) for c in candidates if c.get("content")]
        if not pairs:
            for c in candidates:
                c["reranker_score"] = 0.0
            return candidates[:top_n]

        # Run inference off the event loop to avoid blocking
        loop = asyncio.get_event_loop()
        scores: list[float] = await loop.run_in_executor(
            None,
            lambda: model.predict(pairs).tolist(),  # type: ignore[union-attr]
        )

        # Attach scores — candidates without content get 0.0
        scored: list[dict[str, Any]] = []
        score_idx = 0
        for c in candidates:
            if c.get("content"):
                c["reranker_score"] = round(float(scores[score_idx]), 6)
                score_idx += 1
            else:
                c["reranker_score"] = 0.0
            scored.append(c)

        scored.sort(key=lambda x: x.get("reranker_score", 0.0), reverse=True)
        return scored[:top_n]

    async def _ensure_model(self) -> Any:
        """Load the cross-encoder model with double-checked locking.

        The model is cached at module level so that multiple instances
        using the same model name share a single loaded model.

        Returns:
            The loaded ``sentence_transformers.CrossEncoder`` instance.
        """
        if self._model is not None:
            return self._model

        _MODEL_LOCKS.setdefault(self._model_name, asyncio.Lock())

        async with _MODEL_LOCKS[self._model_name]:
            # Double-check — another coroutine may have loaded it while
            # we were waiting for the lock.
            if self._model_name in _MODEL_CACHE:
                self._model = _MODEL_CACHE[self._model_name]
                return self._model

            try:
                from sentence_transformers import CrossEncoder  # noqa: PLC0415
            except ImportError as err:
                raise ImportError(
                    "sentence-transformers is not installed. "
                    "Install with: pip install openzync[reranker]"
                ) from err

            # Load the model off the event loop
            loop = asyncio.get_event_loop()
            model = await loop.run_in_executor(
                None,
                lambda: CrossEncoder(self._model_name),  # type: ignore[no-any-return,unused-ignore]
            )

            _MODEL_CACHE[self._model_name] = model
            self._model = model
            return self._model
