"""Base worker utilities — retry decorator, bitmask constants, shared helpers.

All ARQ worker task modules import from this module for common patterns:

* Bitmask constants for ``episodes.enrichment_status``.
* ``with_retry`` decorator for exponential-backoff retry logic.
* ``_is_retryable`` predicate used by the retry decorator.

Usage::

    from services.worker.tasks.base import (
        ENRICHMENT_ENTITIES,
        ENRICHMENT_FACTS,
        with_retry,
    )

    @with_retry(max_retries=3, base_delay=2.0)
    async def extract_entities(ctx, episode_id: str):
        ...
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any, Callable

import httpx
import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Bitmask constants for episodes.enrichment_status
# ═══════════════════════════════════════════════════════════════════════════════
# These map one-to-one to enrichment worker tasks.  Each task claims its bit
# atomically via SELECT ... FOR UPDATE + bitwise OR.

ENRICHMENT_ENTITIES: int = 1 << 0
"""Bit 0 — entity extraction task completed."""

ENRICHMENT_EMBEDDING: int = 1 << 1
"""Bit 1 — episode embedding task completed."""

ENRICHMENT_FACTS: int = 1 << 2
"""Bit 2 — fact extraction task completed."""

ENRICHMENT_ENTITY_LINKS: int = 1 << 3
"""Bit 3 — entity-episode linking task completed."""

ENRICHMENT_ALL: int = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_EMBEDDING
    | ENRICHMENT_FACTS
    | ENRICHMENT_ENTITY_LINKS
)
"""Mask with all enrichment bits set — use to check if an episode is fully enriched."""


# ═══════════════════════════════════════════════════════════════════════════════
# Retry decorator
# ═══════════════════════════════════════════════════════════════════════════════


def with_retry(
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> Callable[..., Callable[..., Any]]:
    """Decorator that adds exponential backoff retry to worker tasks.

    Retries only on **retryable** errors:
    - HTTP timeouts (``httpx.TimeoutException``).
    - HTTP 408, 429, 502, 503, 504 status codes.
    - Connection errors and network-level timeouts.

    Does **not** retry on:
    - Validation errors, bad requests (4xx except 408/429).
    - Other non-transient errors.

    Each retry waits ``base_delay * 2^attempt`` seconds (exponential backoff).
    After exhausting ``max_retries`` the last exception is re-raised and
    propagates to the ARQ worker's ``on_job_failed`` callback.

    Args:
        max_retries: Maximum number of retry attempts (default 3).
            The first attempt is **not** counted as a retry.
        base_delay: Initial delay in seconds before the first retry
            (doubles each subsequent attempt).

    Returns:
        A decorator that wraps an async worker function.

    Example::

        @with_retry(max_retries=3, base_delay=2.0)
        async def extract_entities(ctx, episode_id: str) -> None:
            ...

    Raises:
        The last exception encountered if all retries are exhausted.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    if base_delay <= 0:
        raise ValueError("base_delay must be > 0")

    def decorator(
        func: Callable[..., Any],
    ) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            last_error: Exception | None = None

            for attempt in range(max_retries + 1):  # +1 for the initial try
                try:
                    return await func(ctx, *args, **kwargs)
                except Exception as exc:
                    last_error = exc

                    if _is_retryable(exc):
                        if attempt < max_retries:
                            delay = base_delay * (2**attempt)
                            logger.warning(
                                "task.retry",
                                task=func.__name__,
                                attempt=attempt + 1,
                                max_retries=max_retries,
                                delay_seconds=delay,
                                error=str(exc),
                                error_type=type(exc).__name__,
                            )
                            await asyncio.sleep(delay)
                        else:
                            # Final attempt also failed — will re-raise.
                            logger.error(
                                "task.retries_exhausted",
                                task=func.__name__,
                                max_retries=max_retries,
                                error=str(exc),
                                error_type=type(exc).__name__,
                            )
                    else:
                        # Non-retryable error — re-raise immediately.
                        logger.warning(
                            "task.non_retryable_error",
                            task=func.__name__,
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                        raise

            # All retries exhausted — re-raise the last error.
            raise last_error  # type: ignore[misc]  — last_error is set if we get here

        return wrapper

    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# Retryability predicate
# ═══════════════════════════════════════════════════════════════════════════════


def _is_retryable(exc: Exception) -> bool:
    """Determine whether an exception is transient and worth retrying.

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
