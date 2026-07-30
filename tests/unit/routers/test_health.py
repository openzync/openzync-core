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
