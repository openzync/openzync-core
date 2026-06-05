"""ARQ worker configuration — shared settings for all background workers.

Import these settings when creating an ARQ ``Worker`` or when defining the
``WorkerSettings`` class that ARQ discovers via the ``--settings`` CLI flag.

Usage:

    # In a worker entry-point script:
    from services.worker.worker_settings import WorkerSettings

    from arq import create_worker
    worker = create_worker(WorkerSettings)
    worker.run()

The ``functions`` list is intentionally empty here — it is populated in
later phases (Phase 1+) as task functions are implemented.
"""

from __future__ import annotations

from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings

from core.config import settings as app_settings


class WorkerSettings:
    """ARQ worker configuration for OpenZep background jobs.

    All durations are in seconds unless otherwise noted.

    Tweak via environment variables — see :class:`openzep.core.config.Settings`
    for the full list.
    """

    # ── Queue ──────────────────────────────────────────────────────────────
    #: Redis connection details — derived from the app's ``REDIS_URL``.
    redis_settings: ClassVar[RedisSettings] = RedisSettings.from_dsn(
        str(app_settings.REDIS_URL),
    )

    # ── Concurrency ────────────────────────────────────────────────────────
    #: Maximum number of jobs a worker runs concurrently.
    max_jobs: ClassVar[int] = 4

    #: Maximum time (seconds) a job is allowed to run before being killed.
    job_timeout: ClassVar[int] = 300  # 5 minutes

    #: Interval (seconds) between polls for new jobs when the queue is empty.
    poll_delay: ClassVar[float] = 0.5

    # ── Result retention ───────────────────────────────────────────────────
    #: Seconds to keep successful job results.
    keep_result: ClassVar[int] = 3600  # 1 hour

    #: Seconds to keep failed job results (longer for debugging).
    keep_result_failed: ClassVar[int] = 86400  # 24 hours

    # ── Scheduled jobs (cron) ──────────────────────────────────────────────
    #: Cron-style recurring jobs.  Populated in Phase 1+ as features require
    #: periodic maintenance tasks (ephemeral summarisation, TTL cleanup, etc.).
    cron_jobs: ClassVar[list[cron]] = []

    # ── Task registry ──────────────────────────────────────────────────────
    #: List of async functions ARQ will route jobs to.
    #: Populated in Phase 1+ as individual task modules are implemented.
    functions: ClassVar[list] = []

    # ── Startup / shutdown hooks ───────────────────────────────────────────

    @staticmethod
    async def on_startup() -> None:
        """Called once when the worker starts.

        Use this to initialise clients (DB, LLM, etc.) that the worker
        functions depend on.
        """
        # Placeholder — expanded in later phases.
        pass

    @staticmethod
    async def on_shutdown() -> None:
        """Called once before the worker stops.

        Use this to close clients gracefully.
        """
        # Placeholder — expanded in later phases.
        pass
