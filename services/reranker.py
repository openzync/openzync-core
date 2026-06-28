"""Cross-encoder re-ranker — abstract interface, implementations, and factory.

Provides a pluggable re-ranking layer that sits between RRF fusion and
final context assembly.  Two backends are supported:

1. **SentenceTransformersReranker** — local ``sentence-transformers``
   cross-encoder model (default: ``cross-encoder/ms-marco-MiniLM-L-6-v2``).
2. **CohereReranker** — Cohere Rerank API (default: ``rerank-english-v3.0``).

The factory (:class:`RerankerFactory`) builds the appropriate backend from
:class:`schemas.organization_config.OrgConfigBase` and returns ``None``
when re-ranking is not configured.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from schemas.organization_config import OrgConfigBase

# ── Module-level model cache (double-checked locking) ──────────────────────

_MODEL_CACHE: dict[str, Any] = {}
"""Lazy-loaded cross-encoder model instances, keyed by model name."""

_MODEL_LOCKS: dict[str, asyncio.Lock] = {}
"""Per-model-name asyncio locks to prevent concurrent model loading."""

logger = logging.getLogger(__name__)

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


# ── SentenceTransformers (local) implementation ────────────────────────────


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
                    "Install with: pip install openzep[reranker]"
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


# ── Cohere (remote API) implementation ─────────────────────────────────────


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
                "cohere is not installed. Install with: pip install openzep[cohere]"
            ) from err

        self._client = cohere.Client(
            api_key=self._api_key,
            timeout=30,
            max_retries=2,
        )


# ── Factory ────────────────────────────────────────────────────────────────


class RerankerFactory:
    """Factory for constructing a :class:`CrossEncoderReranker` from config.

    Inspects ``OrgConfigBase.reranker_backend`` to decide which backend
    to create.  Returns ``None`` when re-ranking is not configured or
    the required dependencies are not installed.
    """

    @staticmethod
    def create(org_config: OrgConfigBase) -> CrossEncoderReranker | None:
        """Build a re-ranker from an organization's configuration.

        Args:
            org_config: Organization configuration with ``reranker_backend``
                and optional ``reranker_model``, ``reranker_top_k``,
                ``reranker_top_n``, and ``cohere_api_key`` fields.

        Returns:
            A configured re-ranker instance, or ``None`` if the backend
            is not set, unknown, or its dependencies are missing.
        """
        backend = org_config.reranker_backend
        if not backend:
            return None

        if backend == "sentence_transformers":
            return RerankerFactory._create_sentence_transformers(org_config)
        if backend == "cohere":
            return RerankerFactory._create_cohere(org_config)

        logger.warning(
            "reranker.unknown_backend",
            extra={
                "backend": backend,
                "supported": ["sentence_transformers", "cohere"],
            },
        )
        return None

    @staticmethod
    def _create_sentence_transformers(
        org_config: OrgConfigBase,
    ) -> SentenceTransformersReranker | None:
        """Create a local cross-encoder re-ranker.

        Returns ``None`` silently if ``sentence-transformers`` is not
        installed (the caller can fall back gracefully).
        """
        try:
            from sentence_transformers import CrossEncoder  # noqa: F401, PLC0415
        except ImportError:
            logger.warning(
                "reranker.sentence_transformers_not_installed",
                extra={"hint": "pip install openzep[reranker]"},
            )
            return None

        return SentenceTransformersReranker(
            model_name=org_config.reranker_model,
        )

    @staticmethod
    def _create_cohere(org_config: OrgConfigBase) -> CohereReranker | None:
        """Create a Cohere API re-ranker.

        Validates that ``cohere_api_key`` is set and the ``cohere``
        package is installed.  Returns ``None`` if either is missing.
        """
        if not org_config.cohere_api_key:
            logger.warning("reranker.cohere_no_api_key")
            return None

        try:
            import cohere  # noqa: F401, PLC0415
        except ImportError:
            logger.warning(
                "reranker.cohere_not_installed",
                extra={"hint": "pip install openzep[cohere]"},
            )
            return None

        return CohereReranker(
            api_key=org_config.cohere_api_key,
            model_name=org_config.reranker_model,
        )
