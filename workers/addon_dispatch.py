"""Addon dispatch — single ARQ task fanning out to enabled addons."""

from __future__ import annotations

import hashlib
import inspect
import time
from typing import Any

import orjson
import structlog

logger = structlog.get_logger(__name__)

DEDUP_TTL_S = 24 * 3600


def _dedup_key(addon_id: str, op: str, payload: dict[str, Any]) -> str:
    """Build the idempotency key from canonical JSON (stable across runs).

    Args:
        addon_id: Target addon id.
        op: Operation name.
        payload: JSON-serialisable operation arguments.

    Returns:
        The Redis dedup key.
    """
    canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    digest = hashlib.sha256(canonical).hexdigest()[:32]
    return f"addon:dispatch:{addon_id}:{op}:{digest}"


async def addon_dispatch(ctx: dict[str, Any], addon_id: str, op: str, **payload: Any) -> dict[str, Any]:
    """Dispatch one addon operation with 24 h idempotency dedup.

    Fail-closed: unknown or disabled addons raise instead of running.

    Args:
        ctx: ARQ worker context (expects ``redis``).
        addon_id: Target addon id.
        op: Operation name exposed by the addon module.
        payload: JSON-serialisable operation arguments.

    Returns:
        ``{"status": "ok", "result": ...}`` from the addon handler.
    """
    from core.exceptions import AuthorizationError, NotFoundError

    redis = ctx.get("redis")
    if redis is None:
        raise RuntimeError("addon_dispatch requires ctx['redis'].")
    dedup_key = _dedup_key(addon_id, op, payload)
    try:
        claimed = await redis.set(dedup_key, "1", ex=DEDUP_TTL_S, nx=True)
    except Exception as exc:
        logger.error("addon.dispatch_cache_failed", error=str(exc))
        raise
    if not claimed:
        logger.info("addon.dispatch_dedup_hit", key=dedup_key)
        return {"status": "duplicate"}

    # Resolve the addon from the worker-process registry if present;
    # otherwise fail closed — never run an unknown/disabled addon.
    # Ops prefer the record snapshot, then the live module attr, then the
    # register_worker() factory — same precedence as the router resolver.
    records: dict = ctx.get("addons", {})
    record = records.get(addon_id)
    if record is None or not getattr(record, "enabled", False):
        raise AuthorizationError(f"Addon {addon_id!r} is not enabled.")
    from core.addons.hooks import resolve_worker_ops

    ops = getattr(record, "worker_ops", None) or resolve_worker_ops(
        getattr(record, "module")
    )
    handler = (ops or {}).get(op)
    if handler is None:
        raise NotFoundError(f"Addon {addon_id!r} has no op {op!r}.")
    if not callable(handler):
        return {"status": "ok", "result": None}
    started = time.monotonic()
    try:
        # Handlers declaring a ``db``/``session`` parameter receive an
        # addon-only session — bypass-incapable by construction. Core
        # keeps ``RlsAsyncSession``; addon contexts use ONLY this factory.
        params = set(inspect.signature(handler).parameters)
        if params & {"db", "session"}:
            from core.addons.sessions import resolve_addon_factory
            from core.rls import init_context

            factory = resolve_addon_factory(ctx)
            async with factory() as session:
                await init_context(session, None)
                kwargs: dict[str, Any] = dict(payload)
                if "db" in params:
                    kwargs["db"] = session
                if "session" in params:
                    kwargs["session"] = session
                result = await handler(**kwargs) if inspect.iscoroutinefunction(
                    handler
                ) else handler(**kwargs)
        else:
            result = await handler(**payload) if inspect.iscoroutinefunction(
                handler
            ) else handler(**payload)
    finally:
        logger.info(
            "addon.dispatch_done",
            addon=addon_id,
            op=op,
            ms=round((time.monotonic() - started) * 1000),
        )
    return {"status": "ok", "result": result}


async def reconcile_addon_versions(ctx: dict[str, Any]) -> None:
    """Hourly cron — refresh ``latest_seen``/``update_available`` flags."""
    from core.addons.sessions import resolve_addon_factory

    factory = resolve_addon_factory(ctx)
    from repositories.addon_repository import AddonRepository
    from services.addon_service import AddonService

    async with factory() as session:
        service = AddonService(repo=AddonRepository(session), app=ctx.get("app"))
        await service.check_updates()
        await session.commit()
    logger.info("addon.versions_reconciled")


__all__ = ["DEDUP_TTL_S", "addon_dispatch", "reconcile_addon_versions"]
