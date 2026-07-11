"""ARQ (Async Redis Queue) connection management.

Provides a module-level singleton pattern compatible with FastAPI's lifespan:
call ``init_arq(...)`` on startup and ``close_arq()`` on shutdown.  Access the
ready pool via ``get_arq()``.

Usage::

    from core.arq import get_arq

    job_id = await get_arq().enqueue("send_notification", user_id=42)
"""

from __future__ import annotations

import logging
from typing import Any

from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job  # noqa: F401 — re-exported for convenience

from core.config import get_settings

logger = logging.getLogger(__name__)


class ARQPool:
    """Manages the ARQ worker connection pool lifecycle.

    Wraps ``arq.create_pool`` and provides a convenience ``enqueue`` method
    that accepts ``**kwargs`` and returns the enqueued job ID.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        """Initialise — does **not** connect.

        Args:
            redis_url: Redis connection string.  Falls back to
                ``settings.REDIS_URL``.
        """
        self._redis_url: str = redis_url or str(get_settings().REDIS_URL)
        self._pool: Any = None  # arq.connections.ArqRedis (not exported)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Create the ARQ connection pool.

        Raises:
            ConnectionError: If the Redis server is unreachable.
        """
        try:
            redis_settings = RedisSettings.from_dsn(self._redis_url)
            self._pool = await create_pool(redis_settings)
            logger.info(
                "arq.pool_initialized",
                extra={"redis_url": self._redis_url},
            )
        except Exception as exc:
            logger.error(
                "arq.pool_initialization_failed",
                extra={"error": str(exc), "redis_url": self._redis_url},
            )
            raise ConnectionError(
                f"Failed to connect to Redis at {self._redis_url}: {exc}"
            ) from exc

    async def close(self) -> None:
        """Close the connection pool gracefully.

        Safe to call multiple times.
        """
        if self._pool is not None:
            try:
                await self._pool.close()
                await self._pool.wait_closed()
            except Exception as exc:
                logger.warning("arq.pool_close_error", extra={"error": str(exc)})
            finally:
                self._pool = None
                logger.info("arq.pool_closed")

    # ── Accessor ───────────────────────────────────────────────────────────────

    @property
    def pool(self) -> Any:
        """Access the underlying ARQ pool (``ArqRedis``).

        Raises:
            RuntimeError: If ``initialize()`` has not been called.
        """
        if self._pool is None:
            raise RuntimeError("ARQ pool has not been initialised — call initialize() first")
        return self._pool

    # ── Convenience ────────────────────────────────────────────────────────────

    async def enqueue(self, task_name: str, queue_name: str | None = None, **kwargs: Any) -> str | None:
        """Enqueue a background job.

        Args:
            task_name: Name of the registered worker function.
            queue_name: Optional queue name (e.g. "high" or "low"). Passed
                as ``_queue`` to ARQ's ``enqueue_job``.

        Returns:
            The enqueued job ID, or ``None`` if the pool is not available.
        """
        pool = self.pool  # raises RuntimeError if not initialised
        enqueue_kwargs = {"_queue_name": queue_name} if queue_name else {}
        job = await pool.enqueue_job(task_name, **kwargs, **enqueue_kwargs)
        if job is None:
            logger.warning("arq.enqueue_returned_none", extra={"task": task_name})
            return None
        job_id: str = job.job_id
        logger.debug(
            "arq.job_enqueued",
            extra={"task": task_name, "job_id": job_id},
        )
        return job_id


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level singleton — managed by FastAPI lifespan hooks
# ═══════════════════════════════════════════════════════════════════════════════

_pool: ARQPool | None = None


async def init_arq(redis_url: str | None = None) -> ARQPool:
    """Initialise the global ARQ pool singleton.

    Intended to be called from FastAPI's ``lifespan`` startup context.

    Args:
        redis_url: Redis connection string (defaults to
            ``settings.REDIS_URL``).

    Returns:
        The initialised ``ARQPool`` singleton.

    Raises:
        ConnectionError: If Redis is unreachable.
    """
    global _pool
    if _pool is not None:
        logger.warning("arq.reinitialization_attempted — closing existing pool")
        await _pool.close()

    _pool = ARQPool(redis_url=redis_url)
    await _pool.initialize()
    return _pool


async def close_arq() -> None:
    """Shut down the global ARQ pool singleton.

    Safe to call multiple times.
    """
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_arq() -> ARQPool:
    """Retrieve the global ARQ pool singleton.

    Returns:
        The initialised pool.

    Raises:
        RuntimeError: If ``init_arq()`` has not been called.
    """
    if _pool is None:
        raise RuntimeError(
            "ARQ pool has not been initialised — call init_arq() during "
            "application startup before accessing the pool."
        )
    return _pool
