"""Memory service — backward-compatible re-export.

The implementation lives in :mod:`services.memory` (facade
:class:`~services.memory.MemoryService` composing focused mixins).
This module preserves the original import path for routers, factories,
workers, and tests — including ``get_arq``, which the unit tests patch
(the sub-package resolves it through this namespace via deferred
import).
"""

from __future__ import annotations

from core.arq import get_arq
from services.memory import ARQ_QUEUE, ARQ_TASKS, MemoryService

__all__ = ["ARQ_QUEUE", "ARQ_TASKS", "MemoryService", "get_arq"]
