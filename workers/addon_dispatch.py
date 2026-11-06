"""Single stable ARQ entry for every addon task — ``addon_dispatch``.

Registered **once** in the worker's task list; per-addon tasks live in
each record's ``worker_tasks`` map and are resolved at dispatch time, so
hot-loaded addons need no worker restart. Disabled addons reject enqueue
and drain in-flight jobs without side effects. Every execution carries an
idempotency dedup key (``SET NX EX 24h``) — redelivery is a no-op.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import orjson

logger = logging.getLogger(__name__)

DEDUP_PREFIX = "addon:dedup:"
"""Redis prefix for addon task dedup keys."""

DEDUP_TTL_SECONDS = 86400
"""Dedup window — one execution per (addon, task, args) per day."""


def dedup_key(addon_id: str, task: str, args: dict[str, Any]) -> str:
    """Build the idempotency key for one addon task invocation.

    Args:
        addon_id: The addon slug.
        task: The registered task name.
        args: The task arguments (canonically serialised).

    Returns:
        The Redis dedup key.
    """
    canonical = orjson.dumps(args, option=orjson.OPT_SORT_KEYS).decode()
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return f"{DEDUP_PREFIX}{addon_id}:{task}:{digest}"


async def enqueue_addon_task(
    arq_pool: Any, redis: Any, addon_id: str, task: str, args: dict[str, Any]
) -> str | None:
    """Enqueue an addon task, refusing disabled addons at enqueue time.

    Args:
        arq_pool: The ARQ pool (``enqueue_job("addon_dispatch", ...)``).
        redis: Raw Redis client (enabled-flag + dedup checks).
        addon_id: The addon slug.
        task: The registered task name.
        args: JSON-safe task arguments.

    Returns:
        The ARQ job id.

    Raises:
        ValueError: If the addon is not enabled (disabled rejects enqueue).
        ServiceUnavailableError: If the enabled-flag store (Redis) cannot
            be reached — fail-closed deny, never a raw 500.
    """
    from core.addons.loader import REDIS_ENABLED_KEY_PREFIX
    from core.exceptions import ServiceUnavailableError

    if redis is None:
        logger.warning("addon.enqueue_no_redis", extra={"addon_id": addon_id})
        raise ServiceUnavailableError(
            "Addon enable-flag store unavailable — refusing enqueue."
        )
    try:
        flag = await redis.get(f"{REDIS_ENABLED_KEY_PREFIX}{addon_id}")
    except Exception as exc:
        logger.warning(
            "addon.enqueue_redis_unavailable",
            extra={"addon_id": addon_id, "error": str(exc)},
        )
        raise ServiceUnavailableError(
            "Addon enable-flag store unavailable — refusing enqueue."
        ) from exc
    if flag == "0":
        raise ValueError(f"Addon {addon_id!r} is disabled — refusing enqueue.")
    job = await arq_pool.enqueue_job(
        "addon_dispatch",
        addon_id,
        task,
        args,
        _job_id=f"addon-{dedup_key(addon_id, task, args).split(':')[-1]}",
    )
    return job.job_id if job is not None else None


async def addon_dispatch(
    ctx: dict[str, Any], addon_id: str, task: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Execute one addon task with enable-check, dedup, and tenant binding.

    The Redis mirror flag (``addon:enabled:{id}``, written by the API on
    every enable/disable) is authoritative at dispatch: ``"0"`` drains the
    job without side effects even if the worker snapshot predates the
    disable. A missing flag falls back to the startup snapshot
    (fail-closed to disabled when neither knows the addon).

    Args:
        ctx: ARQ worker context (``db_session_factory``, ``redis``,
            ``addon_tasks``, ``addon_state``).
        addon_id: The addon slug.
        task: The registered task name.
        args: JSON-safe task arguments (may carry ``org_id``).

    Returns:
        ``{"status": "ok"|"drained"|"duplicate", ...}`` — never raises for
        disabled/duplicate invocations; unknown tasks raise ``ValueError``.
    """
    from core.addons.loader import REDIS_ENABLED_KEY_PREFIX
    from core.rls import set_tenant_context

    redis = ctx.get("redis")
    if redis is not None:
        try:
            claimed = await redis.set(
                dedup_key(addon_id, task, args), "1", nx=True, ex=DEDUP_TTL_SECONDS
            )
        except Exception:
            claimed = True  # Redis outage must not block execution
        if not claimed:
            logger.info(
                "addon.task_duplicate", extra={"addon_id": addon_id, "task": task}
            )
            return {"status": "duplicate", "addon_id": addon_id, "task": task}

    enabled: bool | None = (ctx.get("addon_state") or {}).get(addon_id)
    if redis is not None:
        try:
            flag = await redis.get(f"{REDIS_ENABLED_KEY_PREFIX}{addon_id}")
        except Exception:
            flag = None
        if flag == "0":
            enabled = False
        elif flag == "1":
            enabled = True
    if enabled is not True:
        logger.info("addon.task_drained", extra={"addon_id": addon_id, "task": task})
        return {"status": "drained", "addon_id": addon_id, "task": task}

    task_fn = (ctx.get("addon_tasks") or {}).get((addon_id, task))
    if task_fn is None:
        raise ValueError(f"Unknown addon task {addon_id!r}:{task!r}.")

    factory = ctx.get("db_session_factory")
    if factory is None:
        raise ValueError("Worker context has no db_session_factory.")
    async with factory() as db:
        org_id = args.get("org_id")
        if org_id is not None:
            await set_tenant_context(db, org_id)
        try:
            result = await task_fn(ctx, db, args)
        except asyncio.CancelledError:
            raise
        return {"status": "ok", "addon_id": addon_id, "task": task, "result": result}
