"""Unit tests for the admin metrics router.

Tests ``/metrics/summary``, ``/metrics/query``, and ``/metrics/targets``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id
from dependencies.db import get_db
from routers.admin_metrics import router, _get_metrics_service
from schemas.admin_metrics import (
    EpisodeStats,
    GraphStats,
    MetricsSummaryResponse,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(autouse=True)
def _stub_permission_gate() -> None:
    """Stub the permission gate for every test in this file.

    The router gates with ``require_permission("members:read")`` — a
    closure created at router import time that cannot be keyed in
    ``dependency_overrides``.  Patching ``dependencies.auth._check_permission``
    (the shared decision function) stubs the gate while keeping the
    ``require_org_id`` chain intact.  The real gate matrix is covered by
    ``test_admin_gate_matrix.py``.
    """
    with patch("dependencies.auth._check_permission", new=AsyncMock()):
        yield


def _create_app() -> tuple[FastAPI, AsyncMock]:
    """Build a minimal FastAPI app with the admin metrics router."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)
    # Ensure execute() returns a sync mock so scalar() yields values, not coroutines
    db_mock.execute.return_value = MagicMock()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

    app.include_router(router)
    return app, db_mock


# ── /metrics/summary ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metrics_summary_success() -> None:
    """GET /metrics/summary returns 200 with aggregated metrics."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    # Mock DB scalar results — _fetch_db_counts calls scalar() 9 times.
    # Each call gets the next value: episodes (6), graph entities (2), users (1).
    # All execute() calls return the same MagicMock (set in _create_app), so
    # scalar.side_effect on the shared return_value distributes values in order.
    db_mock.execute.return_value.scalar.side_effect = [42, 10, 5, 2, 35, 30, 100, 5, 3]

    # Mock MetricsService via its dependency factory
    mock_metrics = AsyncMock()
    mock_metrics.get_summary.return_value = MetricsSummaryResponse(
        episodes=EpisodeStats(
            added_total=42,
            added_24h=10,
            in_progress=5,
            enrichment_pending=2,
            fully_enriched=35,
            with_embeddings=30,
            fully_enriched_pct=83.3,
        ),
        graphs=GraphStats(
            entities_total=100,
            entities_24h=5,
            relationships_total=0,
        ),
        users_total=3,
        request_rate={"2xx": 5.0, "4xx": 0.5, "5xx": 0.1},
        error_rate_pct=1.5,
        status="ok",
    )

    # Override the _get_metrics_service dependency
    app.dependency_overrides[_get_metrics_service] = lambda: mock_metrics

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["episodes"]["added_total"] == 42
    assert body["episodes"]["fully_enriched_pct"] == 83.3
    assert body["graphs"]["entities_total"] == 100
    assert body["users_total"] == 3
    assert body["request_rate"]["2xx"] == 5.0
    mock_metrics.get_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_metrics_summary_no_data() -> None:
    """GET /metrics/summary returns 200 with zeros when no data exists."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    db_mock.execute.return_value.scalar.return_value = 0

    mock_metrics = AsyncMock()
    mock_metrics.get_summary.return_value = MetricsSummaryResponse(
        status="ok",
    )

    app.dependency_overrides[_get_metrics_service] = lambda: mock_metrics

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["episodes"]["added_total"] == 0
    assert body["episodes"]["fully_enriched_pct"] == 0.0
    assert body["graphs"]["entities_total"] == 0
    assert body["users_total"] == 0


# ── /metrics/query ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_org_query_success() -> None:
    """GET /metrics/query returns 200 with predefined query result."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    # Mock DB result for episodes_per_day query — returns rows via scalars()
    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([
        MagicMock(date="2026-08-18", count=42),
        MagicMock(date="2026-08-17", count=38),
    ]))
    db_mock.execute.return_value = mock_result

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/query", params={"query": "episodes_per_day", "days": 7})

    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "episodes_per_day"
    assert body["org_scoped"] is True
    assert "columns" in body
    assert "rows" in body


@pytest.mark.asyncio
async def test_get_org_query_unknown_returns_422() -> None:
    """GET /metrics/query returns 422 for unknown query name."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/query", params={"query": "nonexistent_query"})

    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body


# ── /metrics/targets ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_prometheus_targets_success() -> None:
    """GET /metrics/targets returns 200 with scrape targets."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

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

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get.return_value = mock_response
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/targets")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["targets"]) == 1
    assert body["targets"][0]["job"] == "openzync"
    assert body["targets"][0]["health"] == "up"


@pytest.mark.asyncio
async def test_get_prometheus_targets_empty() -> None:
    """GET /metrics/targets returns 200 with empty list when no targets."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "success",
        "data": {"activeTargets": []},
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get.return_value = mock_response
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/targets")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["targets"]) == 0


@pytest.mark.asyncio
async def test_get_prometheus_targets_502() -> None:
    """GET /metrics/targets returns 502 when Prometheus is unreachable."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/targets")

    assert resp.status_code == 502


# ── 401 auth ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_endpoint_requires_auth() -> None:
    """All /metrics endpoints return 401 when org_id is not provided."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)
    db_mock.execute.return_value = MagicMock()

    # No auth middleware — request.state.org_id will not be set
    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_org_id] = lambda: _raise_401()
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/summary")
    assert resp.status_code == 401


def _raise_401():
    from fastapi import HTTPException, status

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@pytest.mark.unit
def test_metrics_routes_registered_once() -> None:
    """The admin_metrics router is included exactly once in the real app.

    Guards Fix 3a — a duplicate ``include_router(admin_metrics.router)``
    registers the same handlers twice.  FastAPI 0.139 keeps included
    routers as lazy ``_IncludedRouter`` placeholders in ``app.routes``,
    so we assert on both the raw include list (router identity) and the
    resolved effective paths.
    """
    from fastapi.routing import _EffectiveRouteContext, _IncludedRouter

    from services.api.main import create_app

    app = create_app()

    included = [r for r in app.routes if isinstance(r, _IncludedRouter)]
    matches = [r for r in included if r.original_router is router]
    assert len(matches) == 1

    # Resolve the flattened route list and count /metrics-* occurrences.
    paths: list[str] = []

    def _collect(routes: list) -> None:
        for route in routes:
            if isinstance(route, _IncludedRouter):
                for candidate in route.effective_candidates():
                    if isinstance(candidate, _IncludedRouter):
                        _collect([candidate])
                    elif isinstance(candidate, _EffectiveRouteContext):
                        paths.append(candidate.path)
            elif getattr(route, "path", None):
                paths.append(route.path)

    _collect(app.routes)
    metrics_paths = [p for p in paths if p.startswith("/metrics")]
    assert metrics_paths, "expected /metrics-prefixed routes in the app"
    assert len(metrics_paths) == len(set(metrics_paths)), metrics_paths
