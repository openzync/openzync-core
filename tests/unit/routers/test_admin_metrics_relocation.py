"""Regression: admin metrics/stats routers are thin HTTP adapters.

Covers item 1 of the must-fix list:
- ``routers/admin_metrics.py`` + ``routers/admin_stats.py`` contain no
  ``db.execute``/``select()`` — all DB access lives in service+repo.
- ``GET /metrics/summary?days=7`` shape, ``GET /metrics/targets`` shape,
  ``GET /metrics/batch?days=7`` partial envelope ``{results,errors,meta}``.
- 401 without auth, 422 on bad ``days``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException, status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id
from dependencies.db import get_db
from routers import admin_metrics
from routers.admin_metrics import router as metrics_router
from routers.admin_stats import router as stats_router
from schemas.admin_metrics import MetricsSummaryResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _read_router_source(name: str) -> str:
    """Return the raw source of a router module (no import side effects)."""
    return (Path(__file__).resolve().parents[3] / "routers" / name).read_text()


def test_admin_metrics_router_has_no_db_access() -> None:
    """The metrics router never touches the DB — service+repo own queries."""
    src = _read_router_source("admin_metrics.py")
    assert "db.execute" not in src
    assert "select(" not in src


def test_admin_stats_router_has_no_db_access() -> None:
    """The stats router never touches the DB — service+repo own queries."""
    src = _read_router_source("admin_stats.py")
    assert "db.execute" not in src
    assert "select(" not in src


@pytest.fixture(autouse=True)
def _stub_permission_gate():
    """Stub the permission gate (same approach as test_admin_metrics.py)."""
    with patch("dependencies.auth._check_permission", new=AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def _reset_prom_client_singleton():
    admin_metrics._prom_client = None  # noqa: SLF001
    yield
    admin_metrics._prom_client = None  # noqa: SLF001


def _create_app(*, include_stats: bool = False) -> tuple[FastAPI, AsyncMock]:
    """Build a minimal app with the metrics (and optionally stats) router."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)
    db_mock.execute.return_value = MagicMock()
    app.state.openbao_client = AsyncMock()
    app.state.openbao_client.read_org_config = AsyncMock(return_value={})
    app.state.redis = AsyncMock()
    app.state.redis.get = AsyncMock(return_value=None)

    @app.middleware("http")
    async def _mock_auth(request, call_next):  # noqa: ANN001, ANN202
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        return await call_next(request)

    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.include_router(metrics_router)
    if include_stats:
        app.include_router(stats_router)
    return app, db_mock


@pytest.mark.asyncio
async def test_summary_days7_shape_and_service_called() -> None:
    """GET /metrics/summary?days=7 returns the merged shape via the service."""
    app, _ = _create_app()
    mock_service = AsyncMock()
    mock_service.get_summary.return_value = MetricsSummaryResponse(status="ok")
    app.dependency_overrides[admin_metrics._get_admin_metrics_service] = (  # noqa: SLF001
        lambda: mock_service
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/summary", params={"days": 7})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"episodes", "graphs", "users_total", "status"}
    assert body["status"] == "ok"
    # Router delegated to the service layer (service+repo own the queries).
    mock_service.get_summary.assert_awaited_once()
    assert mock_service.get_summary.call_args.args[2] == ORG_ID


@pytest.mark.asyncio
async def test_summary_bad_days_returns_422() -> None:
    """GET /metrics/summary?days=5 is rejected (only 7/30/90 allowed)."""
    app, _ = _create_app()
    app.dependency_overrides[admin_metrics._get_admin_metrics_service] = (  # noqa: SLF001
        lambda: AsyncMock()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/summary", params={"days": 5})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "days must be one of 7, 30, 90."


@pytest.mark.asyncio
async def test_targets_shape() -> None:
    """GET /metrics/targets returns ``{status, targets[]}``."""
    app, _ = _create_app()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "success",
        "data": {
            "activeTargets": [
                {
                    "labels": {"job": "openzync", "instance": "localhost:8000"},
                    "health": "up",
                    "lastScrape": "2024-01-01T00:00:00Z",
                    "lastError": "",
                }
            ]
        },
    }
    mock_prom = AsyncMock()
    mock_prom.get.return_value = mock_response
    transport = ASGITransport(app=app)
    with patch("routers.admin_metrics._get_prom_client", return_value=mock_prom):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/targets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["targets"][0]["job"] == "openzync"
    assert body["targets"][0]["health"] == "up"


@pytest.mark.asyncio
async def test_batch_days7_partial_envelope() -> None:
    """GET /metrics/batch?days=7 returns the partial-OK envelope."""
    app, _ = _create_app()
    mock_result = MagicMock()
    mock_result.__iter__.side_effect = lambda: iter(
        [SimpleNamespace(date="2026-08-18", count=42)]
    )
    db_mock = app.dependency_overrides[get_db]()
    db_mock.execute.return_value = mock_result
    mock_prom = AsyncMock()
    mock_prom.get.side_effect = httpx.ConnectError("down")
    transport = ASGITransport(app=app)
    with patch("routers.admin_metrics._get_prom_client", return_value=mock_prom):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/batch", params={"days": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"results", "errors", "meta"}
    assert body["meta"]["days"] == 7
    assert isinstance(body["meta"]["partial"], bool)
    assert body["results"]["episodes_per_day"]["org_scoped"] is True


@pytest.mark.asyncio
async def test_batch_bad_days_returns_422() -> None:
    """GET /metrics/batch?days=5 is rejected."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/batch", params={"days": 5})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "days must be one of 7, 30, 90."


@pytest.mark.asyncio
async def test_metrics_endpoints_require_auth() -> None:
    """All /metrics endpoints return 401 without an org identity."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)

    def _raise_401() -> str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
        )

    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_org_id] = _raise_401
    app.include_router(metrics_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/metrics/summary", "/metrics/targets", "/metrics/batch?days=7"):
            resp = await client.get(path)
            assert resp.status_code == 401, path


@pytest.mark.asyncio
async def test_admin_stats_org_delegates_to_service() -> None:
    """GET /v1/admin/stats/org?days=7 delegates to AdminStatsService."""
    from routers import admin_stats as admin_stats_mod

    app, _ = _create_app(include_stats=True)
    service_mock = AsyncMock()
    from schemas.admin_stats import OrgStatsResponse

    service_mock.get_org_stats.return_value = OrgStatsResponse(
        organization_id=ORG_ID,
        total_episodes=1,
        total_sessions=0,
        total_facts=0,
        total_extractions=0,
        total_observations=0,
        total_classifications=0,
    )
    app.dependency_overrides[admin_stats_mod._get_admin_stats_service] = (  # noqa: SLF001
        lambda: service_mock
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/admin/stats/org", params={"days": 7})
    assert resp.status_code == 200
    service_mock.get_org_stats.assert_awaited_once()
