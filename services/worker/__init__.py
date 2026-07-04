"""ARQ worker package for OpenZync background tasks.

The worker process is started via:

    python -m services.worker.worker

Configuration is loaded from environment variables through
:class:`WorkerSettings`.  Task functions are registered in
:data:`worker.HIGH_QUEUE_TASKS` and :data:`worker.LOW_QUEUE_TASKS` as they are
implemented (Phase 1b+).
"""

from __future__ import annotations

from services.worker.worker_settings import WorkerSettings, get_queue_name, settings

__all__ = [
    "WorkerSettings",
    "get_queue_name",
    "settings",
]
