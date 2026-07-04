"""Factory for constructing a ``CrossEncoderReranker`` from org-level config.

Inspects ``OrgConfigBase.reranker_backend`` to decide which backend
to create.  Returns ``None`` when re-ranking is not configured or
the required dependencies are not installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from packages.reranker.cohere import CohereReranker
from packages.reranker.sentence_transformers import SentenceTransformersReranker

if TYPE_CHECKING:
    from schemas.organization_config import OrgConfigBase

logger = logging.getLogger(__name__)


class RerankerFactory:
    """Factory for constructing a ``CrossEncoderReranker`` from config.

    Inspects ``OrgConfigBase.reranker_backend`` to decide which backend
    to create.  Returns ``None`` when re-ranking is not configured or
    the required dependencies are not installed.
    """

    @staticmethod
    def create(  # noqa: PLR0911
        org_config: OrgConfigBase,
    ) -> SentenceTransformersReranker | CohereReranker | None:
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
                extra={"hint": "pip install openzync[reranker]"},
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
                extra={"hint": "pip install openzync[cohere]"},
            )
            return None

        return CohereReranker(
            api_key=org_config.cohere_api_key,
            model_name=org_config.reranker_model,
        )
