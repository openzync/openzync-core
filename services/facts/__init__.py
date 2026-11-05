"""Fact invalidation service — facade composing the supersession pipeline.

Sub-package layout (one responsibility per module)::

    conflict_matching.py    identity normalization + match rules
    supersession_ingest.py  deterministic batch ingest state machine
    llm_invalidation.py     LLM contradiction close-out + retraction effects
    fact_persistence.py     inserts, tombstone scans, supersession writes
    post_commit_effects.py  queued webhook/cache/metric/graph-sync effects

``FactInvalidationService`` keeps the original import path and public API
(``ingest_with_supersession``, ``apply_llm_invalidations``,
``notify_retraction``); workers and services are unchanged.
``services/fact_invalidation_service.py`` re-exports it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from repositories.fact_repository import FactRepository
from services.facts.conflict_matching import (
    ConflictMatchingMixin,
    normalize_identity_term,
)
from services.facts.llm_invalidation import LLMInvalidationMixin, LLMInvalidationResult
from services.facts.persistence import FactPersistenceMixin
from services.facts.post_commit_effects import (
    PURGE_ONLY_CACHE_TTL,
    PostCommitEffectsMixin,
    _purge_project_context,
)
from services.facts.supersession_ingest import (
    FactIngestionResult,
    SupersessionIngestMixin,
)

if TYPE_CHECKING:
    from services.cache_service import CacheService
    from services.graph_edge_sync_service import GraphEdgeSyncService
    from services.webhook_service import WebhookService

logger = logging.getLogger(__name__)


class FactInvalidationService(
    ConflictMatchingMixin,
    SupersessionIngestMixin,
    LLMInvalidationMixin,
    FactPersistenceMixin,
    PostCommitEffectsMixin,
):
    """Orchestrates the supersession state machine for a fact batch.

    Args:
        db: An async SQLAlchemy session (transaction owned by the caller).
        fact_repo: Repository providing the conflict scan and
            ``set_valid_to`` primitives.
        webhook_service: Optional — emits ``FACT_SUPERSEDED`` webhooks
            when provided (API path).
        cache_service: Optional — purges the project context-cache
            prefix when provided (API path).
        graph_sync: Optional — synchronises edge expiry into the graph
            backends post-commit per the D1 rule (see
            :class:`GraphEdgeSyncService`).  ``None`` (default) disables
            graph synchronisation.
    """

    def __init__(
        self,
        db: Any,
        fact_repo: FactRepository,
        *,
        webhook_service: WebhookService | None = None,
        cache_service: CacheService | None = None,
        graph_sync: GraphEdgeSyncService | None = None,
    ) -> None:
        self._db = db
        self._fact_repo = fact_repo
        self._webhook_service = webhook_service
        self._cache_service = cache_service
        self._graph_sync = graph_sync
        self._pending_effects: list[Any] = []
        self._events_attached = False


__all__ = [
    "FactIngestionResult",
    "FactInvalidationService",
    "LLMInvalidationResult",
    "PURGE_ONLY_CACHE_TTL",
    "_purge_project_context",
    "normalize_identity_term",
]
