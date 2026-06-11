"""Shared utilities, constants, and decorators for enrichment workers.

All workers in ``workers/tasks/`` import from this module rather than
duplicating retry logic or bitmask constants.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, ParamSpec, TypeVar

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
#   bit 3 = graphiti node sync (sync_to_graph)
#   bit 4 = dialog classification (classify_dialog)
#   bit 5 = structured extraction (extract_structured)

ENRICHMENT_ENTITIES: int = 1 << 0  # bit 0
ENRICHMENT_EMBEDDING: int = 1 << 1  # bit 1
ENRICHMENT_FACTS: int = 1 << 2  # bit 2
ENRICHMENT_SYNC_GRAPH: int = 1 << 3  # bit 3
ENRICHMENT_CLASSIFICATION: int = 1 << 4  # bit 4
ENRICHMENT_STRUCTURED_EXTRACTION: int = 1 << 5  # bit 5

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
) -> Callable[[Callable[F, T]], Callable[F, T]]:
    """Decorator that retries an async function with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (default 3).
        base_delay_s: Initial delay in seconds before the first retry.
        max_delay_s: Maximum delay cap in seconds.
        on_exhaustion: What to do when retries are exhausted.
            ``"raise"`` (default) re-raises the last exception.
            ``"log"`` logs the failure and returns ``None``.

    Returns:
        The decorated function with retry behaviour.

    Raises:
        The last exception if ``on_exhaustion="raise"`` (default).

    Example:
        .. code-block:: python

            @with_retry(max_retries=3, base_delay_s=0.5)
            async def fetch_llm(prompt: str) -> str:
                return await client.complete(prompt)
    """
    if on_exhaustion not in ("raise", "log"):
        raise ValueError(f"on_exhaustion must be 'raise' or 'log', got {on_exhaustion!r}")

    def decorator(func: Callable[F, T]) -> Callable[F, T]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            delay = base_delay_s

            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "worker.retry",
                        extra={
                            "function": func.__name__,
                            "attempt": attempt,
                            "max_retries": max_retries,
                            "error": str(exc),
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
