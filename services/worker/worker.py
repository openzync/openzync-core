"""ARQ worker entrypoint.

Runs background tasks defined in ``WorkerSettings.functions``.
The worker is started via:

    python -m services.worker.worker

or inside the ``services/worker/`` Docker container.

Worker functions are populated in Phase 1 as domain features
(memory consolidation, embedding generation, etc.) are implemented.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from arq import create_pool
from arq.connections import RedisSettings

from core.config import settings as app_settings

logger = structlog.get_logger()


# ── Lifecycle hooks ─────────────────────────────────────────────────────────


async def startup(ctx: dict[str, Any]) -> None:
    """Called once when the worker starts.

    Initialises the application ``Settings`` and stores it in the
    worker context so that worker functions can access configuration
    without re-reading environment variables.

    Args:
        ctx: Mutable worker context dict shared across all functions.
    """
    ctx["settings"] = app_settings
    logger.info("worker.started", redis_url=str(app_settings.REDIS_URL))


async def shutdown(ctx: dict[str, Any]) -> None:  # noqa: ARG001
    """Called once when the worker stops.

    Args:
        ctx: Worker context dict (unused on shutdown).
    """
    logger.info("worker.stopped")


async def health_check(ctx: dict[str, Any]) -> bool:  # noqa: ARG001
    """Return worker health status.

    Args:
        ctx: Worker context dict (unused).

    Returns:
        Always ``True`` — the worker is healthy if it is running.
    """
    return True


# ── Worker configuration ────────────────────────────────────────────────────


class WorkerSettings:
    """ARQ worker configuration.

    Attributes are consumed directly by ``arq.Worker``.  See the
    `ARQ documentation <https://arq-docs.helpmanual.io/>`_ for details.
    """

    functions: list = []
    """List of async worker functions to register. Populated in Phase 1."""

    redis_settings: RedisSettings | None = None
    """Redis connection settings. Set at runtime before calling run()."""

    max_jobs: int = 4
    """Maximum number of concurrent jobs this worker handles."""

    job_timeout: int = 300
    """Maximum job runtime in seconds before the job is timed out."""

    poll_delay: float = 0.5
    """Seconds between polls when the queue is empty."""

    keep_result: int = 3600
    """Seconds to keep successful job results."""

    keep_result_failed: int = 86400
    """Seconds to keep failed job results."""

    on_startup: callable = startup
    """Async callback invoked when the worker starts."""

    on_shutdown: callable = shutdown
    """Async callback invoked when the worker stops."""

    health_check: callable = health_check
    """Async function that returns ``True`` when the worker is healthy."""


# ── CLI entrypoint ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    settings = app_settings
    WorkerSettings.redis_settings = RedisSettings.from_dsn(
        str(settings.REDIS_URL)
    )
    logger.info(
        "worker.starting",
        concurrency=WorkerSettings.max_jobs,
    )

    # create_pool returns an asyncio-compatible connection pool.
    # In production the ARQ CLI (``arq services.worker.worker.WorkerSettings``)
    # handles the event loop; this manual path is for containerised use.
    asyncio.run(create_pool(WorkerSettings.redis_settings))
