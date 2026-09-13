"""Health-check endpoints for liveness and readiness probes.

- ``GET /health`` — lightweight liveness check (always returns 200).
- ``GET /ready`` — readiness check that validates connectivity to
  PostgreSQL and Redis.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core._version import __version__
from schemas.health import HealthResponse

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
    db_health = await _check_db_health(request)
    redis_health = await _check_redis_health(request)
    """Readiness probe — validates all downstream dependencies.

    Returns:
        - ``200`` with ``"status": "ok"`` when every check passes.
        - ``503`` with ``"status": "degraded"`` and per-check results
          when one or more dependencies are unreachable.
    """
    checks = {
        "database": db_health,
        "redis": redis_health,
    }
    all_healthy = all(checks.values())

    return JSONResponse(
        content={
            "status": "ok" if all_healthy else "degraded",
            "checks": checks,
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

