"""Degraded /ready on Redis kill — 200/503 with detail, never 500."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_ready_degraded_never_500() -> None:
    from core.config import Settings, get_settings, set_settings

    try:
        get_settings()
    except RuntimeError:
        set_settings(Settings(DATABASE_URL="postgresql+asyncpg://localhost/x", REDIS_URL="redis://localhost/0", SECRET_KEY="i" * 32, WEBHOOK_SIGNING_SECRET="j" * 32))
    from services.api.main import app

    app.state.db_engine = object()
    from unittest.mock import AsyncMock

    broken = AsyncMock()
    broken.ping.side_effect = ConnectionError("redis down")
    app.state.redis = broken

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")
    assert resp.status_code in (200, 503)
    assert resp.status_code != 500
    body = resp.json()
    assert "status" in body
    assert "checks" in body or "degraded" in body
