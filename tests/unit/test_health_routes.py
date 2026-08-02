"""Unit tests for health-router registration.

Regression test for the root-level mount fix: the health router must be
registered WITHOUT the ``/v1`` prefix (see ``services/api/main.py``), so
Kubernetes/Helm probes, NGINX, and the Dockerfile healthcheck resolve
``/health`` and ``/ready`` at root instead of 404ing.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

# NOTE: `services.api.main` is imported inside each test, not at module
# level — the module executes ``app = create_app()`` at import time, which
# calls ``get_settings()``. The unit conftest's autouse ``_init_settings``
# fixture initialises the settings singleton before each test body runs.


async def _get(path: str) -> int:
    """GET ``path`` against the real app (lifespan not run)."""
    from services.api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(path)
        return resp.status_code


async def test_health_is_served_at_root() -> None:
    """GET /health must resolve — regression for the removed /v1 prefix."""
    assert await _get("/health") == 200


async def test_ready_is_served_at_root() -> None:
    """GET /ready must resolve (never 404).

    The readiness endpoint reads ``app.state.db_engine``/``redis``, which the
    lifespan normally populates — skipped here. Stub with inert objects: both
    health-check helpers catch exceptions and report the dependency unhealthy,
    so the endpoint returns 503 (degraded), not an unhandled error.
    """
    from services.api.main import app

    app.state.db_engine = object()
    app.state.redis = object()

    status = await _get("/ready")
    assert status in (200, 503)


async def test_health_response_contract() -> None:
    """GET /health returns the documented liveness body."""
    from services.api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["service"] == "openzync-api"
    assert "version" in body
