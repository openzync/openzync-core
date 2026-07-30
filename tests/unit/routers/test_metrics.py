"""Unit tests for the Prometheus metrics endpoint.

Tests ``GET /metrics`` — unauthenticated, returns Prometheus text format.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CONTENT_TYPE_LATEST

from routers.metrics import router


def _create_app() -> FastAPI:
    """Build a minimal FastAPI app with only the metrics router.

    The /metrics endpoint has no auth dependencies — it must be
    accessible by Prometheus scrapers without bearer tokens.
    """
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_get_metrics_success() -> None:
    """GET /metrics returns 200 with Prometheus text/plain content type."""
    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == CONTENT_TYPE_LATEST
    assert len(resp.text) > 0
    # Prometheus format is line-based: each line is a metric or comment
    lines = resp.text.strip().split("\n")
    assert len(lines) >= 1


@pytest.mark.asyncio
async def test_get_metrics_no_auth_required() -> None:
    """GET /metrics succeeds without any auth headers (public endpoint)."""
    app = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    # No auth-related error should appear in the response
    assert "authentication" not in resp.text.lower()
    assert "unauthorized" not in resp.text.lower()
