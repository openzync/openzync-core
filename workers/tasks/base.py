"""Shared utilities, constants, and decorators for enrichment workers.

All workers in ``workers/tasks/`` import from this module rather than
duplicating retry logic or bitmask constants.

Bitmask constants are re-exported from :mod:`core.enrichment_bits` (the
canonical home — dependency-free so ``core/`` can import it too).
``services.worker.tasks.base`` re-exports from here as a compatibility shim.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from core.enrichment_bits import (  # noqa: F401 — re-export, canonical home
    ENRICHMENT_ALL,
    ENRICHMENT_BLOB_TEXT,
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_ENTITY_LINKS,
    ENRICHMENT_FACTS,
    ENRICHMENT_OBSERVATIONS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    LLM_INVALIDATION_BIT,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

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


def with_task_metrics(func: Callable[F, T]) -> Callable[F, T]:
    """Record per-job task metrics on the ARQ worker context.

    ARQ hands the **same** context dict to the task function and to the
    worker's ``on_job_end`` callback, but it only ever populates
    ``job_id``, ``job_try``, ``enqueue_time`` and ``score``.  Without this
    decorator every job is reported as ``task_type="unknown"`` with
    ``status="success"`` and a zero duration, whatever actually happened.

    Apply it as the **outermost** decorator (listed above ``@with_retry``)
    so the recorded runtime covers every retry attempt and the status
    reflects the final outcome of the job, not of the last attempt.

    Args:
        func: The task coroutine.  Its first parameter must be the ARQ
            context dict (``ctx``).

    Returns:
        The wrapped coroutine.
    """
    signature = inspect.signature(func)
    ctx_param_name: str | None = next(iter(signature.parameters), None)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        params = signature.bind_partial(*args, **kwargs).arguments
        ctx: Any = params.get(ctx_param_name) if ctx_param_name else None
        if not isinstance(ctx, dict):
            ctx = params.get("ctx")
        started = time.perf_counter()

        if isinstance(ctx, dict):
            ctx["task_type"] = func.__name__
            user_id = params.get("user_id") or params.get("user_id")
            if user_id:
                ctx["user_id"] = str(user_id)
            trace_id = params.get("trace_id")
            if trace_id:
                ctx["trace_id"] = str(trace_id)

        try:
            result = await func(*args, **kwargs)
        except BaseException:
            status = "failure"
            raise
        else:
            status = "success"
        finally:
            if isinstance(ctx, dict):
                ctx["status"] = status
                ctx["runtime"] = time.perf_counter() - started

        return result

    return wrapper  # type: ignore[return-value]


# ── Public API ───────────────────────────────────────────────────────────────

__all__ = [
    "ENRICHMENT_ALL",
    "ENRICHMENT_CLASSIFICATION",
    "ENRICHMENT_EMBEDDING",
    "ENRICHMENT_ENTITIES",
    "ENRICHMENT_ENTITY_LINKS",
    "ENRICHMENT_FACTS",
    "ENRICHMENT_BLOB_TEXT",
    "ENRICHMENT_OBSERVATIONS",
    "ENRICHMENT_STRUCTURED_EXTRACTION",
    "LLM_INVALIDATION_BIT",
    "with_retry",
    "with_task_metrics",
]
