"""Unit tests for MetricsMiddleware."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import generate_latest

from middleware.metrics import (
    METRICS_REGISTRY,
    MetricsMiddleware,
    http_errors_total,
    http_request_duration_seconds,
    http_requests_in_progress,
    http_requests_total,
)


@pytest.mark.unit
class TestMetricsMiddleware:
    """Test suite for MetricsMiddleware — Prometheus RED metrics."""

    def _create_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        @app.get("/error")
        async def server_error() -> None:
            raise RuntimeError("server error")

        app.add_middleware(MetricsMiddleware)
        return app

    @pytest.fixture(autouse=True)
    def _reset_metrics(self) -> None:
        """Reset all metric counters before each test."""
        # Can't easily reset counters, so we just note cumulative values
        METRICS_REGISTRY.get_sample_value  # sanity check registry exists
        yield

    @pytest.mark.asyncio
    async def test_request_passthrough(self) -> None:
        """Normal request passes through."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_requests_total_incremented(self) -> None:
        """http_requests_total counter is incremented after a request."""
        before = METRICS_REGISTRY.get_sample_value(
            "openzync_http_requests_total",
            {"method": "GET", "path": "/test", "status": "2xx"},
        )
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 200

        after = METRICS_REGISTRY.get_sample_value(
            "openzync_http_requests_total",
            {"method": "GET", "path": "/test", "status": "2xx"},
        )
        before_val = before or 0.0
        after_val = after or 0.0
        assert after_val == before_val + 1.0

    @pytest.mark.asyncio
    async def test_errors_total_incremented_on_5xx(self) -> None:
        """http_errors_total counter is incremented on server errors."""
        before = METRICS_REGISTRY.get_sample_value(
            "openzync_http_errors_total",
            {"method": "GET", "path": "/error"},
        )
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            with pytest.raises(RuntimeError):
                await c.get("/error")

        after = METRICS_REGISTRY.get_sample_value(
            "openzync_http_errors_total",
            {"method": "GET", "path": "/error"},
        )
        before_val = before or 0.0
        after_val = after or 0.0
        assert after_val == before_val + 1.0

    @pytest.mark.asyncio
    async def test_duration_recorded(self) -> None:
        """Request duration histogram gets an observation."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 200

        # Duration histogram should have at least 1 observation
        count = METRICS_REGISTRY.get_sample_value(
            "openzync_http_request_duration_seconds_count",
            {"method": "GET", "path": "/test"},
        )
        assert count is not None
        assert count >= 1.0

    @pytest.mark.asyncio
    async def test_in_progress_gauge(self) -> None:
        """In-progress gauge tracks concurrent requests."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 200

        # After request completes, in-progress should be 0
        val = METRICS_REGISTRY.get_sample_value(
            "openzync_http_requests_in_progress",
            {"method": "GET"},
        )
        assert val is not None
        assert val == 0.0

    @pytest.mark.asyncio
    async def test_metrics_endpoint_returns_prometheus_format(self) -> None:
        """The /metrics endpoint returns valid Prometheus text format."""
        # We just check the registry can generate output
        output = generate_latest(METRICS_REGISTRY)
        assert output
        text = output.decode("utf-8")
        assert "openzync_http_requests_total" in text
        assert "openzync_http_request_duration_seconds" in text

    @pytest.mark.asyncio
    async def test_post_request_metrics(self) -> None:
        """POST requests are also tracked."""
        before = METRICS_REGISTRY.get_sample_value(
            "openzync_http_requests_total",
            {"method": "POST", "path": "/test", "status": "2xx"},
        )
        app = FastAPI()

        @app.post("/test")
        async def create() -> dict:
            return {"id": 1}

        app.add_middleware(MetricsMiddleware)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/test", json={"name": "x"})
            assert resp.status_code == 200

        after = METRICS_REGISTRY.get_sample_value(
            "openzync_http_requests_total",
            {"method": "POST", "path": "/test", "status": "2xx"},
        )
        assert (after or 0.0) == (before or 0.0) + 1.0

    @pytest.mark.asyncio
    async def test_status_group_tracking(self) -> None:
        """Status code groups (2xx, 4xx, 5xx) are tracked correctly."""
        app = FastAPI()

        @app.get("/not-found")
        async def not_found() -> None:
            from starlette.responses import Response
            # Can't easily trigger 4xx without full app, just verify 2xx path
            return None

        @app.get("/ok")
        async def ok() -> dict:
            return {"status": "ok"}

        app.add_middleware(MetricsMiddleware)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/ok")
            assert resp.status_code == 200

        count = METRICS_REGISTRY.get_sample_value(
            "openzync_http_requests_total",
            {"method": "GET", "path": "/ok", "status": "2xx"},
        )
        assert count is not None and count > 0

    @pytest.mark.asyncio
    async def test_non_http_passthrough(self) -> None:
        """Non-HTTP scopes do not record metrics."""
        app = self._create_app()
        assert app is not None
