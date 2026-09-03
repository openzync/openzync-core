"""Unit tests for the health-check router.

Tests ``GET /health`` (liveness) and ``GET /ready`` (readiness) endpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routers.health import router

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _create_app() -> FastAPI:
    """Build a minimal FastAPI app with only the health router."""
    app = FastAPI()

    # Attach mock dependencies to app.state (required by /ready)
    app.state.db_engine = AsyncMock()
    app.state.redis = AsyncMock()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_health_success() -> None:
    """GET /health returns 200 with status='ok' and service name."""
    app = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "openzync-api"
    assert isinstance(body["version"], str)
    assert len(body["version"]) > 0


@pytest.mark.asyncio
async def test_readiness_ok() -> None:
    """GET /ready returns 200 with status='ok' when all deps are healthy."""
    app = _create_app()
    transport = ASGITransport(app=app)

    with (
        patch("routers.health._check_db_health", return_value=True),
        patch("routers.health._check_redis_health", return_value=True),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] is True
    assert body["checks"]["redis"] is True


@pytest.mark.asyncio
async def test_readiness_degraded_db() -> None:
    """GET /ready returns 503 with degraded status when DB is down."""
    app = _create_app()
    transport = ASGITransport(app=app)

    with (
        patch("routers.health._check_db_health", return_value=False),
        patch("routers.health._check_redis_health", return_value=True),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] is False
    assert body["checks"]["redis"] is True


@pytest.mark.asyncio
async def test_readiness_degraded_redis() -> None:
    """GET /ready returns 503 with degraded status when Redis is down."""
    app = _create_app()
    transport = ASGITransport(app=app)

    with (
        patch("routers.health._check_db_health", return_value=True),
        patch("routers.health._check_redis_health", return_value=False),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] is True
    assert body["checks"]["redis"] is False


@pytest.mark.asyncio
async def test_readiness_both_degraded() -> None:
    """GET /ready returns 503 when both dependencies are down."""
    app = _create_app()
    transport = ASGITransport(app=app)

    with (
        patch("routers.health._check_db_health", return_value=False),
        patch("routers.health._check_redis_health", return_value=False),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] is False
    assert body["checks"]["redis"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 5x-boot live contract — root paths, exact payloads (verified 2026-09-03)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_exact_live_contract() -> None:
    """GET /health returns the exact live payload incl. the shipped version.

    Live: ``200 {"status":"ok","service":"openzync-api","version":"1.0.0rc1"}``.
    ``__version__`` is patched to the observed build version so the test
    pins the contract shape without coupling to the installed dist version.
    """
    import routers.health as health_module

    app = _create_app()
    transport = ASGITransport(app=app)
    with patch.object(health_module, "__version__", "1.0.0rc1"):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "service": "openzync-api",
        "version": "1.0.0rc1",
    }


@pytest.mark.asyncio
async def test_readiness_exact_live_contract() -> None:
    """GET /ready returns the exact live payload when all deps are healthy.

    Live: ``200 {"status":"ok","checks":{"database":true,"redis":true}}``.
    """
    app = _create_app()
    transport = ASGITransport(app=app)

    with (
        patch("routers.health._check_db_health", return_value=True),
        patch("routers.health._check_redis_health", return_value=True),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "checks": {"database": True, "redis": True},
    }


@pytest.mark.asyncio
async def test_health_paths_live_at_root_not_v1() -> None:
    """Probes live at ``/health`` and ``/ready`` — never under ``/v1``.

    Helm/NGINX probes target the root paths (see ``main.py`` router
    registration); the versioned prefix must 404.
    """
    app = _create_app()
    transport = ASGITransport(app=app)

    with (
        patch("routers.health._check_db_health", return_value=True),
        patch("routers.health._check_redis_health", return_value=True),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/ready")).status_code == 200
            assert (await client.get("/v1/health")).status_code == 404
            assert (await client.get("/v1/ready")).status_code == 404


def test_health_router_has_no_prefix() -> None:
    """The health router itself declares root paths (no ``prefix="/v1"``)."""
    paths = sorted({getattr(route, "path", "") for route in router.routes})
    assert paths == ["/health", "/ready"]
