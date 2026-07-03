"""Compatibility shim — imports from the single source of truth.

All shared utilities (``with_retry``, ``ENRICHMENT_*`` constant) have been
consolidated into :mod:`workers.tasks.base`.

This module re-exports those names for backward compatibility and keeps
:func:`_is_retryable` (HTTP-specific predicate used by service workers).

Usage::

    from services.worker.tasks.base import with_retry, ENRICHMENT_ENTITIES
    # is equivalent to
    from workers.tasks.base import with_retry, ENRICHMENT_ENTITIES

New code should import directly from ``workers.tasks.base``.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx
import structlog

# Imported from workers.tasks.base — this module is a compatibility shim.
from workers.tasks.base import (  # noqa: F401  — re-export
    ENRICHMENT_ALL,
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_ENTITY_LINKS,
    ENRICHMENT_FACTS,
    ENRICHMENT_OBSERVATIONS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    with_retry,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Retryability predicate — HTTP-specific, belongs here
# ═══════════════════════════════════════════════════════════════════════════════


def _is_retryable(exc: Exception) -> bool:
    """Determine whether an exception is transient and worth retrying.

    This is an HTTP-specific predicate for use with
    :func:`workers.tasks.base.with_retry` via the ``is_retryable``
    parameter::

        @with_retry(max_retries=3, is_retryable=_is_retryable)
        async def call_api(...):
            ...

    Transient (retryable) exceptions include:
    - HTTP timeouts (``httpx.TimeoutException``).
    - HTTP 408 (Request Timeout), 429 (Too Many Requests / rate-limit),
      502 (Bad Gateway), 503 (Service Unavailable), 504 (Gateway Timeout).
    - Connection errors (DNS resolution failure, connection refused, etc.).
    - Any exception whose string representation contains "timeout" or
      "connection" as a heuristic for network-level failures.

    Args:
        exc: The exception to classify.

    Returns:
        ``True`` if the error is likely transient and retrying may help.
        ``False`` for permanent errors (validation, auth, 4xx except 408/429).
    """
    if isinstance(exc, httpx.TimeoutException):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 429, 502, 503, 504)

    # Connection errors including DNS, refused, reset.
    if isinstance(exc, httpx.ConnectError):
        return True

    # Heuristic: catch-all for network-level issues that don't have a
    # specific httpx wrapper (e.g., `asyncio.TimeoutError` wrapping).
    exc_lower = str(exc).lower()
    if "timeout" in exc_lower or "connection" in exc_lower:
        return True

    return False


# ── Re-export for convenience ────────────────────────────────────────────────

__all__ = [
    "ENRICHMENT_ALL",
    "ENRICHMENT_CLASSIFICATION",
    "ENRICHMENT_EMBEDDING",
    "ENRICHMENT_ENTITIES",
    "ENRICHMENT_ENTITY_LINKS",
    "ENRICHMENT_FACTS",
    "ENRICHMENT_OBSERVATIONS",
    "ENRICHMENT_STRUCTURED_EXTRACTION",
    "_is_retryable",
    "with_retry",
]
