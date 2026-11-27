"""Integration tests for one-time platform setup (first-run bootstrap).

Real PostgreSQL 15 + Redis via testcontainers, per-test isolation
through ``isolated_app`` (table truncation + Redis flush on teardown),
HTTP via ``httpx.AsyncClient`` + ASGI transport. Status codes and
bodies only — no timing assertions.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository
from tests.integration.conftest import asgi_transport

pytestmark = pytest.mark.integration

STRONG_PASSWORD = "StrongPass1"  # noqa: S106 — test fixture password
GENERIC_FORBIDDEN = "Platform setup is not available."


@pytest_asyncio.fixture(loop_scope="function")
async def anon_client(isolated_app: Any, engine: Any, redis_client: Any) -> Any:
    """Unauthenticated HTTP client backed by the isolated app."""
    from core.db import get_async_session

    isolated_app.state.db_engine = engine
    isolated_app.state.db_session_factory = get_async_session(engine)
    isolated_app.state.redis = redis_client
    isolated_app.state.degraded = {}
    async with AsyncClient(
        transport=asgi_transport(isolated_app), base_url="http://test"
    ) as client:
        yield client


async def test_ready_fresh_db_reports_needs_setup(
    anon_client: AsyncClient,
) -> None:
    """Fresh DB → 503 with needs_setup + degraded.platform_seed."""
    resp = await anon_client.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["needs_setup"] is True
    assert body["degraded"]["platform_seed"]
    assert body["status"] == "degraded"


async def test_setup_full_flow(
    anon_client: AsyncClient, isolated_app: Any
) -> None:
    """Setup → SYSTEM org + root rows → /ready clean → root login."""
    resp = await anon_client.post(
        "/v1/auth/setup", json={"password": STRONG_PASSWORD}
    )
    assert resp.status_code == 201
    assert "root" in resp.json()["message"]

    async with isolated_app.state.db_session_factory() as session:
        org = await OrganizationRepository(session).get_platform_org()
        assert org is not None
        assert org.name == "Platform"
        root = await UserRepository(session).get_platform_root()
        assert root is not None
        assert root.role == "admin"

    with patch("routers.health.is_model_loaded", return_value=True):
        ready = await anon_client.get("/ready")
    assert ready.status_code == 200
    ready_body = ready.json()
    assert ready_body["needs_setup"] is False
    assert "platform_seed" not in ready_body["degraded"]

    login = await anon_client.post(
        "/v1/auth/login",
        json={"email": "root", "password": STRONG_PASSWORD},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await anon_client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["role"] == "admin"
    assert me_body["is_email_verified"] is True
    assert me_body["must_change_password"] is False


async def test_second_setup_forbidden_with_identical_body(
    anon_client: AsyncClient,
) -> None:
    """Post-setup calls → 403 generic, byte-identical bodies."""
    first = await anon_client.post(
        "/v1/auth/setup", json={"password": STRONG_PASSWORD}
    )
    assert first.status_code == 201

    second = await anon_client.post(
        "/v1/auth/setup", json={"password": STRONG_PASSWORD}
    )
    third = await anon_client.post(
        "/v1/auth/setup", json={"password": STRONG_PASSWORD}
    )

    assert second.status_code == 403
    assert third.status_code == 403
    assert second.json() == third.json()
    assert second.json()["detail"] == GENERIC_FORBIDDEN


async def test_setup_weak_password_returns_422(
    anon_client: AsyncClient,
) -> None:
    """Schema-short and validator-weak passwords both → 422."""
    short = await anon_client.post(
        "/v1/auth/setup", json={"password": "weak"}
    )
    assert short.status_code == 422

    weak = await anon_client.post(
        "/v1/auth/setup", json={"password": "weakpass1"}
    )
    assert weak.status_code == 422


async def test_concurrent_double_setup_single_winner(
    anon_client: AsyncClient,
) -> None:
    """Two racing setups → exactly one 201 and one generic 403."""
    results = await asyncio.gather(
        anon_client.post(
            "/v1/auth/setup", json={"password": STRONG_PASSWORD}
        ),
        anon_client.post(
            "/v1/auth/setup", json={"password": STRONG_PASSWORD}
        ),
    )

    assert sorted(r.status_code for r in results) == [201, 403]
    loser = next(r for r in results if r.status_code == 403)
    assert loser.json()["detail"] == GENERIC_FORBIDDEN


async def test_setup_throttle_burst_returns_429_with_headers(
    anon_client: AsyncClient,
) -> None:
    """Signup-IP burst past the limit → 429 carrying rate-limit headers."""
    responses = [
        await anon_client.post(
            "/v1/auth/setup", json={"password": STRONG_PASSWORD}
        )
        for _ in range(8)
    ]

    throttled = [r for r in responses if r.status_code == 429]
    assert throttled
    assert "x-ratelimit-limit" in throttled[0].headers
