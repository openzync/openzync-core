"""Shared utilities, constants, and decorators for enrichment workers.

All workers in ``workers/tasks/`` import from this module rather than
duplicating retry logic or bitmask constants.

This module also serves as the single source of truth imported by
:mod:`services.worker.tasks.base` (a compatibility shim).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# ── Enrichment-status bitmask constants ───────────────────────────────────────
# These correspond to the ``episodes.enrichment_status`` integer bitmask column.
# Each bit represents one enrichment step.  Workers check their bit before
# running and set it after completion (or after a permanent failure).

# note: Bit positions are shared across the team and must not be
# reassigned without updating all workers.  Current allocation:
#   bit 0 = entity extraction (extract_entities)
#   bit 1 = embedding generation (embed_episode)
#   bit 2 = fact extraction (extract_facts)
# bit 3 = entity-episode linking (link_entities_to_episode)
#   bit 4 = dialog classification (classify_dialog)
#   bit 5 = structured extraction (extract_structured)
#   bit 6 = deferred graph-topology observations (compute_observations)
#           NOTE: bit 6 is RESERVED but NOT included in ENRICHMENT_ALL.
#           The observations pass is non-blocking and deferred — including
#           it in the ALL mask would gate "fully enriched" status on an
#           unimplemented worker.

ENRICHMENT_ENTITIES: int = 1 << 0  # bit 0
ENRICHMENT_EMBEDDING: int = 1 << 1  # bit 1
ENRICHMENT_FACTS: int = 1 << 2  # bit 2
ENRICHMENT_ENTITY_LINKS: int = 1 << 3  # bit 3
ENRICHMENT_CLASSIFICATION: int = 1 << 4  # bit 4
ENRICHMENT_STRUCTURED_EXTRACTION: int = 1 << 5  # bit 5
ENRICHMENT_OBSERVATIONS: int = 1 << 6  # bit 6 — reserved, not in ALL

ENRICHMENT_ALL: int = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_EMBEDDING
    | ENRICHMENT_FACTS
    | ENRICHMENT_ENTITY_LINKS
    | ENRICHMENT_CLASSIFICATION
    | ENRICHMENT_STRUCTURED_EXTRACTION
)
"""Bitmask with all active enrichment bits set.
Use this to check if an episode is fully enriched.

Bit 6 (``ENRICHMENT_OBSERVATIONS``) is intentionally excluded — the
observations pass is non-blocking and deferred.
"""

# ── Default retry configuration ──────────────────────────────────────────────
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BASE_DELAY_S: float = 1.0
DEFAULT_MAX_DELAY_S: float = 30.0


F = ParamSpec("F")
T = TypeVar("T")


def with_retry(
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    *,
    on_exhaustion: str = "raise",
    is_retryable: Callable[[Exception], bool] | None = None,
) -> Callable[[Callable[F, T]], Callable[F, T]]:
    """Decorator that retries an async function with exponential backoff.

    By default (when ``is_retryable`` is ``None``) **all** exceptions are
    retried.  Pass a predicate to limit retries to transient errors only::

        @with_retry(is_retryable=_is_retryable)
        async def call_external_api(...):
            ...

    Args:
        max_retries: Maximum number of retry attempts (default 3).
        base_delay_s: Initial delay in seconds before the first retry.
        max_delay_s: Maximum delay cap in seconds.
        on_exhaustion: What to do when retries are exhausted.
            ``"raise"`` (default) re-raises the last exception.
            ``"log"`` logs the failure and returns ``None``.
        is_retryable: Optional predicate that receives the caught exception
            and returns ``True`` if a retry should be attempted.  When
            ``None`` (default) all exceptions are retried.

    Returns:
        The decorated function with retry behaviour.

    Raises:
        The last exception if ``on_exhaustion="raise"`` (default).
        A non-retryable exception immediately if ``is_retryable`` is set
        and returns ``False``.

    Example:
        .. code-block:: python

            @with_retry(max_retries=3, base_delay_s=0.5)
            async def fetch_llm(prompt: str) -> str:
                return await client.complete(prompt)
    """
    if on_exhaustion not in ("raise", "log"):
        raise ValueError(
            f"on_exhaustion must be 'raise' or 'log', got {on_exhaustion!r}"
        )

    def decorator(func: Callable[F, T]) -> Callable[F, T]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            delay = base_delay_s

            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    # When a predicate is provided, check if the error is
                    # worth retrying.  Non-retryable errors propagate
                    # immediately so the caller can handle them.
                    if is_retryable is not None and not is_retryable(exc):
                        logger.warning(
                            "worker.non_retryable",
                            extra={
                                "function": func.__name__,
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )
                        raise

                    last_exc = exc
                    logger.warning(
                        "worker.retry",
                        extra={
                            "function": func.__name__,
                            "attempt": attempt,
                            "max_retries": max_retries,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                            "delay_s": round(delay, 2),
                        },
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, max_delay_s)

            # All retries exhausted
            if on_exhaustion == "log":
                logger.error(
                    "worker.retry_exhausted",
                    extra={
                        "function": func.__name__,
                        "max_retries": max_retries,
                        "error": str(last_exc),
                    },
                )
                return None

            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


# ── Public API ───────────────────────────────────────────────────────────────

__all__ = [
    "ENRICHMENT_ALL",
    "ENRICHMENT_CLASSIFICATION",
    "ENRICHMENT_EMBEDDING",
    "ENRICHMENT_ENTITIES",
    "ENRICHMENT_ENTITY_LINKS",
    "ENRICHMENT_FACTS",
    "ENRICHMENT_OBSERVATIONS",
    "ENRICHMENT_STRUCTURED_EXTRACTION",
    "with_retry",
]
