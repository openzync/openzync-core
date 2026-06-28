"""Re-ranker package — pluggable cross-encoder re-ranking backends.

This package provides:

- ``CrossEncoderReranker`` — Abstract interface for re-ranker backends.
- ``SentenceTransformersReranker`` — Local cross-encoder model.
- ``CohereReranker`` — Cohere Rerank API.
- ``RerankerFactory`` — Config-driven factory for building a re-ranker.

Usage::

    from packages.reranker import (
        CohereReranker,
        CrossEncoderReranker,
        RerankerFactory,
        SentenceTransformersReranker,
        DEFAULT_RERANK_TOP_K,
        DEFAULT_RERANK_TOP_N,
        RRF_K,
    )
"""

from __future__ import annotations

from packages.reranker.cohere import CohereReranker
from packages.reranker.factory import RerankerFactory
from packages.reranker.interface import (
    DEFAULT_RERANK_TOP_K,
    DEFAULT_RERANK_TOP_N,
    RRF_K,
    CrossEncoderReranker,
)
from packages.reranker.sentence_transformers import SentenceTransformersReranker

__all__ = [
    "CohereReranker",
    "CrossEncoderReranker",
    "DEFAULT_RERANK_TOP_K",
    "DEFAULT_RERANK_TOP_N",
    "RRF_K",
    "RerankerFactory",
    "SentenceTransformersReranker",
]
