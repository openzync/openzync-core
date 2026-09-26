"""Canonical embedding policy — one frozen model, one frozen dimension.

pgvector's ``VECTOR(N)`` is fixed-dimension per column, so per-org
configurable dimensions forced unindexable ``float8[]``/``Text`` storage
plus runtime ``CAST(... AS VECTOR(dim))``. Frozen since migration 0054:
every embedding row is 768-dimensional, stored natively as
``VECTOR(768)`` with HNSW cosine indexes.

Rules enforced through this module:

- Write paths (``embed_episode`` / ``embed_fact`` workers,
  ``HybridRetriever._embed_query``) embed with the canonical model and
  refuse to store anything whose length is not
  :data:`CANONICAL_EMBED_DIM` — fail loud via
  :class:`core.exceptions.ExternalServiceError`, never store.
- ``embedding_backend`` stays per-org configurable for provider routing,
  but only dim-compatible providers survive the connection probe.
- ``OllamaBackend`` (``nomic-embed-text``, 768 dims) is the sanctioned
  dim-compatible dev fallback; everything else uses the canonical model.
"""

from __future__ import annotations

from core.exceptions import ExternalServiceError

CANONICAL_EMBED_MODEL: str = "snowflake-arctic-embed-m-v1.5"
"""The single embedding model all stored vectors are produced with."""

CANONICAL_EMBED_DIM: int = 768
"""The single embedding dimension. Matches ``VECTOR(768)`` DDL."""

OLLAMA_DEV_EMBED_MODEL: str = "nomic-embed-text"
"""Dim-compatible (768) dev fallback served by a local Ollama instance."""

_BACKEND_EMBED_MODELS: dict[str, str] = {"ollama": OLLAMA_DEV_EMBED_MODEL}
"""Per-backend model overrides. Anything not listed uses the canonical model."""


def resolve_embed_model(backend: str | None) -> str:
    """Return the frozen embedding model for a provider.

    Args:
        backend: The configured ``embedding_backend`` (``None`` → canonical).

    Returns:
        ``nomic-embed-text`` for Ollama, otherwise the canonical model.
    """
    if backend is not None and backend in _BACKEND_EMBED_MODELS:
        return _BACKEND_EMBED_MODELS[backend]
    return CANONICAL_EMBED_MODEL


def validate_embedding_dim(vec: list[float], *, source: str) -> None:
    """Reject any embedding that is not exactly canonical-dim.

    Args:
        vec: The embedding vector returned by the provider.
        source: Caller name for the error detail (e.g. ``"embed_fact"``).

    Raises:
        ExternalServiceError: If ``len(vec) != CANONICAL_EMBED_DIM`` or any
            element is not a float/int.
    """
    if len(vec) != CANONICAL_EMBED_DIM or not all(
        isinstance(v, (float, int)) and not isinstance(v, bool) for v in vec
    ):
        raise ExternalServiceError(
            message=(
                f"Invalid embedding in {source}: len {len(vec)} "
                f"(expected canonical {CANONICAL_EMBED_DIM} float elements). "
                "Refusing to store."
            ),
            detail={
                "source": source,
                "got": len(vec),
                "expected": CANONICAL_EMBED_DIM,
            },
        )
