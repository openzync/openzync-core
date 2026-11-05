"""Hybrid retriever — backward-compatible re-export.

The implementation lives in :mod:`services.retrieval` (facade
:class:`~services.retrieval.HybridRetriever` composing focused mixins,
with SQL in :class:`repositories.retrieval_repository.RetrievalRepository`).
This module preserves the original import path for routers and tests —
including the module-level names the unit tests patch (``logger``,
``set_tenant_context``, ``release_tenant_context``,
``FactRepository``, ``EpisodeRepository``). The sub-package resolves
these through this namespace (deferred import) so patches take effect.
"""

from __future__ import annotations

import logging

from core.rls import release_tenant_context, set_tenant_context
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from services.retrieval import HybridRetriever
from services.retrieval.lexical_graph_search import MAX_BFS_RESULTS

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_BFS_RESULTS",
    "EpisodeRepository",
    "FactRepository",
    "HybridRetriever",
    "logger",
    "release_tenant_context",
    "set_tenant_context",
]
