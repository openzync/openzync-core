"""Unit tests for the admin metrics router.

Tests ``/metrics/summary``, ``/metrics/batch``, and ``/metrics/targets``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_user_id
from dependencies.db import get_db
from routers import admin_metrics
from routers.admin_metrics import _get_metrics_service, router
from schemas.admin_metrics import (
    EpisodeStats,
    GraphStats,
    MetricsSummaryResponse,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


def _within(*, hours: int) -> str:
    """ISO timestamp *hours* in the past — shapes the 24h entity window."""
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


@pytest.fixture(autouse=True)
def _reset_prom_client_singleton() -> None:
    """Reset the lazy ``_prom_client`` singleton around every test.

    ``routers.admin_metrics._get_prom_client`` caches a module-global
    ``httpx.AsyncClient`` — without a reset, a client injected in one test
    leaks into later tests (empty-targets saw 1 target, 502 saw 200).
    """
    admin_metrics._prom_client = None  # noqa: SLF001
    yield
    admin_metrics._prom_client = None  # noqa: SLF001


def _create_app() -> tuple[FastAPI, AsyncMock]:
    """Build a minimal FastAPI app with the admin metrics router."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)
    # Ensure execute() returns a sync mock so scalar() yields values, not coroutines
    db_mock.execute.return_value = MagicMock()
    # /metrics/summary and /metrics/batch depend on dependencies.platform_config,
    # which reads app.state.redis with a DB fallback (users.config_override).
    # Same app.state wiring as test_admin_gate_matrix / test_admin_system;
    # an empty stored config resolves to the PlatformConfigBase defaults
    # (graph_backend="falkordb").
    app.state.redis = AsyncMock()
    app.state.redis.get = AsyncMock(return_value=None)

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.user_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_user_id] = lambda: str(USER_ID)

    app.include_router(router)
    return app, db_mock


# ── /metrics/summary ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metrics_summary_success() -> None:
    """GET /metrics/summary returns 200 with aggregated metrics."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    # Mock DB scalar results — _fetch_db_counts calls scalar() 9 times in
    # order: total, 24h, archived, enrichable, in_progress, pending,
    # fully_enriched, with_embeddings, users (episodes 8, users 1).
    # added_total (42) == enrichable (40) + archived (2) by construction.
    # Graph entity counts no longer come from SQL
    # (GraphStatsService reads the graph backend, not the never-written
    # graph_entities PG stub), so they are driven by the fake backend below.
    # All execute() calls return the same MagicMock (set in _create_app), so
    # scalar.side_effect on the shared return_value distributes values in order.
    db_mock.execute.return_value.scalar.side_effect = [42, 10, 2, 40, 5, 2, 35, 30, 3]

    # Project scope: GraphStatsService.resolve_project_ids pages through
    # ProjectRepository.list() — one project here, so the backend is fanned
    # out over exactly that project.
    db_mock.execute.return_value.scalars.return_value.all.return_value = [
        SimpleNamespace(id=PROJECT_ID)
    ]

    # Graph backend: 100 entities, 5 created within the last 24h.
    # resolve_and_create is a SYNC factory (core/graph_backend.py:98).
    backend = AsyncMock()
    app.state.graph_backend_dispatcher = MagicMock()
    app.state.graph_backend_dispatcher.resolve_and_create = MagicMock(
        return_value=backend
    )
    backend.get_all_entities = AsyncMock(
        return_value=[
            {"id": f"e{i}", "created_at": _within(hours=1 if i < 5 else 48)}
            for i in range(100)
        ]
    )

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
            fully_enriched_pct=87.5,
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
    # Rebased denominator: fully_enriched (35) / enrichable (40), not total (42).
    assert body["episodes"]["fully_enriched_pct"] == 87.5
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


# ── /metrics/batch ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metrics_batch_success() -> None:
    """GET /metrics/batch returns 200 with the partial-OK envelope."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    # Every DB handler iterates its ``db.execute()`` result; one shared
    # mock serves all 12 concurrent handlers, so __iter__ hands out a
    # fresh (date, count) iterator per call.
    mock_result = MagicMock()
    mock_result.__iter__.side_effect = lambda: iter(
        [
            SimpleNamespace(date="2026-08-18", count=42),
            SimpleNamespace(date="2026-08-17", count=38),
        ]
    )
    db_mock.execute.return_value = mock_result

    # Prometheus is external I/O — fail it fast so the 3 Prom handlers
    # land in ``errors`` (partial-OK) instead of hitting the network.
    mock_prom = AsyncMock()
    mock_prom.get.side_effect = httpx.ConnectError("Connection refused")

    with patch("routers.admin_metrics._get_prom_client", return_value=mock_prom):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/batch", params={"days": 7})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"results", "errors", "meta"}
    entry = body["results"]["episodes_per_day"]
    assert entry["query"] == "episodes_per_day"
    assert entry["user_scoped"] is True
    assert entry["columns"] == ["date", "count"]
    assert entry["rows"] == [["2026-08-18", 42], ["2026-08-17", 38]]
    assert entry["total"] == 2
    assert "parameters" in entry
    assert body["meta"]["days"] == 7
    assert body["meta"]["partial"] is True
    # Partial-OK: the Prometheus handlers failed into ``errors`` (HTTP 200).
    assert {
        "error_rate_by_day",
        "latency_percentiles",
        "context_retrieval_rate",
    } <= {e["query"] for e in body["errors"]}


@pytest.mark.asyncio
async def test_get_metrics_batch_invalid_days_returns_422() -> None:
    """GET /metrics/batch returns 422 when days is not one of 7/30/90."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/batch", params={"days": 5})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "days must be one of 7, 30, 90."


@pytest.mark.asyncio
async def test_get_metrics_batch_days_and_range_returns_422() -> None:
    """GET /metrics/batch returns 422 when days is combined with from/to."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/metrics/batch",
            params={"days": 7, "from": "2026-08-01", "to": "2026-08-08"},
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Pass either days or from/to, not both."


@pytest.mark.asyncio
async def test_get_metrics_batch_from_without_to_returns_422() -> None:
    """GET /metrics/batch returns 422 when from is passed without to."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/batch", params={"from": "2026-08-01"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Pass either days or both from and to."


@pytest.mark.asyncio
async def test_get_metrics_batch_invalid_project_id_returns_422() -> None:
    """Non-UUID project_id → 422 (typed ``UUID | None`` Query validation)."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/metrics/batch",
            params={"days": 7, "project_id": "not-a-uuid"},
        )

    assert resp.status_code == 422
    # FastAPI's request-validation payload names the offending query param.
    errors = resp.json()["detail"]
    assert any(err.get("loc") == ["query", "project_id"] for err in errors), errors


@pytest.mark.asyncio
async def test_get_metrics_batch_with_valid_project_id_returns_200() -> None:
    """Valid UUID project_id is passed through to the handler (still 200).

    ``resolve_platform_scope`` owner-validates ``project_id`` for every
    caller — the repository is stubbed with a project owned by the
    caller so this pins the happy path, not the ownership gate.
    """
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    # Same row shape as test_get_metrics_batch_success — the handler
    # iterates `result`.
    mock_result = MagicMock()
    mock_result.__iter__.side_effect = lambda: iter(
        [
            SimpleNamespace(date="2026-08-18", count=42),
        ]
    )
    db_mock.execute.return_value = mock_result

    mock_prom = AsyncMock()
    mock_prom.get.side_effect = httpx.ConnectError("Connection refused")

    owned_repo = AsyncMock()
    owned_repo.get_by_id = AsyncMock(
        return_value=SimpleNamespace(user_id=USER_ID, id=PROJECT_ID)
    )
    with (
        patch(
            "dependencies.auth.has_platform_manage",
            new=AsyncMock(return_value=False),
        ),
        patch("dependencies.auth.ProjectRepository", return_value=owned_repo),
        patch("routers.admin_metrics._get_prom_client", return_value=mock_prom),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/metrics/batch",
                params={
                    "days": 7,
                    "project_id": str(PROJECT_ID),
                },
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["results"]["episodes_per_day"]["query"] == "episodes_per_day"
    assert body["results"]["episodes_per_day"]["rows"] == [["2026-08-18", 42]]


@pytest.mark.asyncio
async def test_get_metrics_batch_with_foreign_project_id_returns_403() -> None:
    """A real project owned by another user → 403 (Gap-2 owner check)."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    mock_result = MagicMock()
    mock_result.__iter__.side_effect = lambda: iter(
        [
            SimpleNamespace(date="2026-08-18", count=42),
        ]
    )
    db_mock.execute.return_value = mock_result

    mock_prom = AsyncMock()
    mock_prom.get.side_effect = httpx.ConnectError("Connection refused")

    foreign_repo = AsyncMock()
    foreign_repo.get_by_id = AsyncMock(
        return_value=SimpleNamespace(
            user_id=UUID("00000000-0000-0000-0000-000000000099"),
            id=PROJECT_ID,
        )
    )
    with (
        patch(
            "dependencies.auth.has_platform_manage",
            new=AsyncMock(return_value=False),
        ),
        patch("dependencies.auth.ProjectRepository", return_value=foreign_repo),
        patch("routers.admin_metrics._get_prom_client", return_value=mock_prom),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/metrics/batch",
                params={
                    "days": 7,
                    "project_id": str(PROJECT_ID),
                },
            )

    assert resp.status_code == 403


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

    mock_prom = AsyncMock()
    mock_prom.get.return_value = mock_response

    with (
        # ``/metrics/targets`` stays members:read-gated (unlike summary/batch).
        patch("dependencies.auth._check_permission", new=AsyncMock()),
        patch("routers.admin_metrics._get_prom_client", return_value=mock_prom),
    ):
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

    mock_prom = AsyncMock()
    mock_prom.get.return_value = mock_response

    with (
        # ``/metrics/targets`` stays members:read-gated (unlike summary/batch).
        patch("dependencies.auth._check_permission", new=AsyncMock()),
        patch("routers.admin_metrics._get_prom_client", return_value=mock_prom),
    ):
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

    mock_prom = AsyncMock()
    mock_prom.get.side_effect = httpx.ConnectError("Connection refused")

    with (
        # ``/metrics/targets`` stays members:read-gated (unlike summary/batch).
        patch("dependencies.auth._check_permission", new=AsyncMock()),
        patch("routers.admin_metrics._get_prom_client", return_value=mock_prom),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/targets")

    assert resp.status_code == 502


# ── 401 auth ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_endpoint_requires_auth() -> None:
    """All /metrics endpoints return 401 when user_id is not provided."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)
    db_mock.execute.return_value = MagicMock()

    # No auth middleware — request.state.user_id will not be set
    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_user_id] = lambda: _raise_401()
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
    try:
        from fastapi.routing import _EffectiveRouteContext, _IncludedRouter
    except ImportError:
        pytest.skip("FastAPI routing internals differ from expected version")

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
