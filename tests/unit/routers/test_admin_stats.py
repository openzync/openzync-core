"""Unit tests for the admin stats router (thin HTTP adapter).

Tests cover endpoints under ``/v1/admin/stats``:
- ``GET /v1/admin/stats`` — platform aggregate statistics
- ``GET /v1/admin/stats/usage`` — daily usage trends with optional days param

The router delegates to ``AdminStatsService`` (injected via
``_get_admin_stats_service``); all DB access lives in the service.  Tests
mock the service at the router boundary and assert status codes, shapes,
and auth gating.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routers.admin_stats import _get_admin_stats_service, router
from schemas.admin_stats import OrgStatsResponse, UsageStatsResponse

USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _stats_response() -> OrgStatsResponse:
    return OrgStatsResponse(
        user_id=USER_ID,
        total_episodes=100,
        total_sessions=25,
        total_facts=50,
        total_extractions=7,
        total_observations=30,
        total_classifications=12,
    )


def _build_app(service: AsyncMock) -> FastAPI:
    """Create a minimal FastAPI app with the stats router."""
    from dependencies.auth import require_user_id
    from dependencies.db import get_db

    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[require_user_id] = lambda: str(USER_ID)
    app.dependency_overrides[_get_admin_stats_service] = lambda: service
    app.include_router(router)
    return app


# ── GET /v1/admin/stats — platform aggregate stats ───────────────────────────


class TestGetPlatformStats:
    """GET /v1/admin/stats — aggregate counts for the dashboard."""

    @pytest.mark.asyncio
    async def test_returns_platform_stats(self) -> None:
        """Should return aggregate counts for all six metric categories."""
        service = AsyncMock()
        service.get_platform_stats.return_value = _stats_response()
        app = _build_app(service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/admin/stats")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user_id"] == str(USER_ID)
        assert body["total_episodes"] == 100
        assert body["total_sessions"] == 25
        assert body["total_facts"] == 50
        assert body["total_extractions"] == 7
        assert body["total_observations"] == 30
        assert body["total_classifications"] == 12
        service.get_platform_stats.assert_awaited_once()
        call_kwargs = service.get_platform_stats.call_args
        assert call_kwargs.args[0] == USER_ID

    @pytest.mark.asyncio
    async def test_requires_auth(self) -> None:
        """No auth → 401 before the service runs."""
        from dependencies.db import get_db

        app = FastAPI()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[_get_admin_stats_service] = (
            lambda: AsyncMock()
        )
        app.include_router(router)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/admin/stats")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_windowed_by_days(self) -> None:
        """days param is accepted and forwarded (window resolution)."""
        service = AsyncMock()
        service.get_platform_stats.return_value = _stats_response()
        app = _build_app(service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/admin/stats?days=7")

        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_rejects_bad_days_422(self) -> None:
        """days=0 and days=366 fail query validation."""
        service = AsyncMock()
        app = _build_app(service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/v1/admin/stats?days=0")).status_code == 422
            assert (await client.get("/v1/admin/stats?days=366")).status_code == 422
            assert (await client.get("/v1/admin/stats?days=abc")).status_code == 422


# ── GET /v1/admin/stats/usage — daily usage trends ───────────────────────────


class TestGetUsageStats:
    """GET /v1/admin/stats/usage — daily usage rows."""

    @pytest.mark.asyncio
    async def test_returns_usage_rows(self) -> None:
        """Should return the service's daily usage list."""
        service = AsyncMock()
        service.get_usage_stats.return_value = [
            UsageStatsResponse(date="2026-11-19", episode_count=3),
            UsageStatsResponse(date="2026-11-18", episode_count=5),
        ]
        app = _build_app(service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/admin/stats/usage?days=7")

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body) == 2
        assert body[0]["date"] == "2026-11-19"
        service.get_usage_stats.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_bad_days_422(self) -> None:
        """days=0 fails query validation."""
        service = AsyncMock()
        app = _build_app(service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (
                await client.get("/v1/admin/stats/usage?days=0")
            ).status_code == 422
