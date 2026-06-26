"""ARQ task definitions — registered in WorkerSettings.functions.

Task modules are imported into this package and exposed to the ARQ worker
via the ``HIGH_QUEUE_TASKS`` and ``LOW_QUEUE_TASKS`` lists defined below.

The worker entrypoint (``services.worker.worker``) reads these lists when
constructing the ARQ ``Worker`` instances.

Usage in ``services/worker/worker.py``::

    from services.worker.tasks import HIGH_QUEUE_TASKS, LOW_QUEUE_TASKS

    high_worker = create_arq_worker(
        queue_name="high",
        functions=HIGH_QUEUE_TASKS,
        ...
    )

Task signature convention
-------------------------
Every ARQ task is an ``async`` function whose first argument is the ARQ
``ctx`` dict (contains ``redis``, ``job_id``, ``job_try``, and other
ARQ-provided fields).  Additional keyword arguments are deserialised from
whatever was passed to ``enqueue_job()``::

    async def my_task(ctx, episode_id: str, org_id: str, **kwargs) -> None:
        ...
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

# ──────────────────────────────────────────────────────────────────────────────
# Task registry
# ──────────────────────────────────────────────────────────────────────────────
# These lists are populated as task modules are implemented in Phase 1c and
# Phase 2.  Each list entry is an async callable that accepts ``(ctx, **kwargs)``
# and is registered with the ARQ worker.
#
# TASKS_HIGH:  Real-time ingestion tasks (entity extraction, embedding, graph
#              sync, fact extraction, classification).
# TASKS_LOW:   Batch / scheduled tasks (community summarisation, data ingestion,
#              entity merging, context cache refresh, GDPR purges).

TASKS_HIGH: list[Callable[..., Awaitable[Any]]] = []
"""Tasks registered with the **high-priority** ARQ queue.

These tasks run immediately after an episode is ingested.  They are
latency-sensitive (should complete within seconds).  High-priority tasks
share the episode's ``(org_id, user_id, session_id)`` context.
"""

TASKS_LOW: list[Callable[..., Awaitable[Any]]] = []
"""Tasks registered with the **low-priority** ARQ queue.

These tasks run on a schedule or when queue pressure permits.  They are
not latency-sensitive.  Examples: community summarisation, entity merging,
data ingestion for business data, GDPR purges.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Import pattern for Phase 1c+ (uncomment when task modules exist)
# ──────────────────────────────────────────────────────────────────────────────
# To register a task, import the function and append it to the appropriate list:
#
#   from services.worker.tasks.extract_entities import extract_entities
#   from services.worker.tasks.embed_episode import embed_episode
#   from services.worker.tasks.extract_facts import extract_facts
#   from services.worker.tasks.link_entities_to_episode import link_entities_to_episode
#   from services.worker.tasks.classify_dialog import classify_dialog
#   from services.worker.tasks.extract_structured import extract_structured
#   from services.worker.tasks.summarise_community import summarise_community
#   from services.worker.tasks.ingest_business_data import ingest_business_data
from services.worker.tasks.audit_log import write_audit_log
#   from services.worker.tasks.merge_duplicate_entities import merge_duplicate_entities
from workers.email_jobs import send_verification_email
#   from services.worker.tasks.refresh_context_cache import refresh_context_cache
#   from services.worker.tasks.delete_user_data import delete_user_data
#
#   TASKS_HIGH.extend([
#       extract_entities,
#       embed_episode,
#       extract_facts,
#       link_entities_to_episode,
#       classify_dialog,
#       extract_structured,
#   ])
TASKS_LOW.extend([
    write_audit_log,
    send_verification_email,
])
#   TASKS_LOW.extend([
#       summarise_community,
#       ingest_business_data,
#       merge_duplicate_entities,
#       refresh_context_cache,
#       delete_user_data,
#   ])

# ──────────────────────────────────────────────────────────────────────────────
# Re-exports for convenience
# ──────────────────────────────────────────────────────────────────────────────

__all__ = [
    "TASKS_HIGH",
    "TASKS_LOW",
]
