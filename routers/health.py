"""Health-check endpoints for liveness and readiness probes.

- ``GET /health`` — lightweight liveness check (always returns 200).
- ``GET /ready`` — readiness check that validates connectivity to
  PostgreSQL and Redis, plus the prewarmed embedder, aux-component
  degradation, and the stale graph-edge backlog count.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core._version import __version__
from core.embeddings import is_model_loaded
from schemas.health import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Liveness ────────────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe.

    Returns a simple 200 response when the service process is running.
    Does **not** check downstream dependencies — that is the job of
    ``/ready``.
    """
    return HealthResponse(status="ok", service="openzync-api", version=__version__)


# ── Readiness (check all dependencies) ──────────────────────────────────────


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe — validates all downstream dependencies.

    Returns:
        - ``200`` with ``"status": "ok"`` when every check passes.
        - ``503`` with ``"status": "degraded"`` and per-check results
          when one or more dependencies are unreachable.
    """
    db_health = await _check_db_health(request)
    redis_health = await _check_redis_health(request)
    checks = {
        "database": db_health,
        "redis": redis_health,
        # 503 until the boot-time embedder prewarm completes; /health stays
        # liveness-only. Lifespan prewarm runs before serving traffic, so a
        # False here means startup was bypassed (e.g. tests) — fail closed.
        "embeddings": is_model_loaded(),
    }
    # Aux outages (FalkorDB, Ollama, Transit, seed) degrade instead of
    # blocking boot — /ready reports them here until they recover.
    degraded: dict[str, str] = dict(getattr(request.app.state, "degraded", {}))
    all_healthy = all(checks.values()) and not degraded

    return JSONResponse(
        content={
            "status": "ok" if all_healthy else "degraded",
            "checks": checks,
            "degraded": degraded,
            "pending_edge_sync": await _pending_edge_sync_count(request),
        },
        status_code=200 if all_healthy else 503,
    )


# ── Dependency helpers ──────────────────────────────────────────────────────


async def _check_db_health(request: Request) -> bool:
    """Ping the PostgreSQL database via the application's engine."""
    from core.db import check_db_health as _check

    return await _check(request.app.state.db_engine)


async def _check_redis_health(request: Request) -> bool:
    """Ping the Redis server via the application's client."""
    from core.redis import check_redis_health as _check

    return await _check(request.app.state.redis)


async def _pending_edge_sync_count(request: Request) -> int | None:
    """Count stale graph edges awaiting expiry (best-effort).

    ``None`` when the count cannot be computed — /ready reports null
    instead of failing the probe.
    """
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        return None
    try:
        from core import rls
        from repositories.graph_relationship_repository import (
            GraphRelationshipRepository,
        )

        async with factory() as session:
            # Platform-wide scan — same bypass as the reconcile cron.
            await rls.set_bypass_rls(session, True)
            return await GraphRelationshipRepository(session).count_stale_edges()
    except Exception:
        logger.exception("readiness.pending_edge_sync_failed")
        return None

