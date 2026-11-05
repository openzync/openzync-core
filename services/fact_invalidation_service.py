"""Fact invalidation service — backward-compatible re-export.

The implementation lives in :mod:`services.facts` (facade
:class:`~services.facts.FactInvalidationService` composing focused
mixins). This module preserves the original import path for services,
workers, and tests — including ``facts_superseded_total``, which the
unit tests patch (the sub-package resolves it through this namespace
via deferred import).
"""

from __future__ import annotations

from middleware.metrics import facts_superseded_total
from services.facts import (
    PURGE_ONLY_CACHE_TTL,
    FactIngestionResult,
    FactInvalidationService,
    LLMInvalidationResult,
    _purge_project_context,
    normalize_identity_term,
)

__all__ = [
    "PURGE_ONLY_CACHE_TTL",
    "FactIngestionResult",
    "FactInvalidationService",
    "LLMInvalidationResult",
    "_purge_project_context",
    "facts_superseded_total",
    "normalize_identity_term",
]
