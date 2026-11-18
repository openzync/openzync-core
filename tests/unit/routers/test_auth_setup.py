"""Unit tests for POST /v1/auth/setup — one-time bootstrap router.

Pins the exception mapping: service success → 201, service
ConflictError → generic 403 (no bootstrap-state leak), and
service/schema validation failures → 422.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.exceptions import (
    ConflictError,
    ValidationError,
    register_exception_handlers,
)
from dependencies.services import get_auth_throttle, get_setup_service
from routers.auth import router

pytestmark = pytest.mark.unit

GENERIC_FORBIDDEN = "Platform setup is not available."


def _create_app() -> tuple[FastAPI, dict[str, AsyncMock]]:
    """Build a minimal app with the auth router and mocked setup deps."""
    app = FastAPI()
    register_exception_handlers(app)
    mocks: dict[str, AsyncMock] = {}

    mocks["setup_service"] = AsyncMock()
    mocks["auth_throttle"] = AsyncMock()

    app.dependency_overrides[get_setup_service] = (
        lambda: mocks["setup_service"]
    )
    app.dependency_overrides[get_auth_throttle] = (
        lambda: mocks["auth_throttle"]
    )

    app.include_router(router)
    return app, mocks


async def test_setup_returns_201() -> None:
    """Fresh platform → 201 with the root-login confirmation."""
    app, mocks = _create_app()
    mocks["setup_service"].create_root_once.return_value = None

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/auth/setup", json={"password": "StrongPass1"}
        )

    assert resp.status_code == 201
    assert "root" in resp.json()["message"]
    mocks["setup_service"].create_root_once.assert_awaited_once_with(
        "StrongPass1"
    )
    mocks["auth_throttle"].check_signup_attempt.assert_awaited_once()


async def test_setup_conflict_maps_to_generic_403() -> None:
    """Service ConflictError → 403 with the generic message (no leak)."""
    app, mocks = _create_app()
    mocks["setup_service"].create_root_once.side_effect = ConflictError(
        "internal: root row already exists"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/auth/setup", json={"password": "StrongPass1"}
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == GENERIC_FORBIDDEN
    assert "root row" not in resp.text


async def test_setup_403_bodies_are_identical() -> None:
    """Distinct internal causes → byte-identical 403 problem bodies."""
    app, mocks = _create_app()
    transport = ASGITransport(app=app)

    bodies = []
    for cause in ("internal: org exists", "internal: root exists"):
        mocks["setup_service"].create_root_once.side_effect = ConflictError(
            cause
        )
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/auth/setup", json={"password": "StrongPass1"}
            )
        assert resp.status_code == 403
        bodies.append(resp.json())

    assert bodies[0] == bodies[1]
    assert bodies[0]["status"] == 403


async def test_setup_missing_password_returns_422() -> None:
    """Empty payload → 422 without reaching the service."""
    app, mocks = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post("/v1/auth/setup", json={})

    assert resp.status_code == 422
    mocks["setup_service"].create_root_once.assert_not_awaited()


async def test_setup_short_password_returns_422() -> None:
    """Schema min_length rejects short passwords → 422, no service call."""
    app, mocks = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/auth/setup", json={"password": "short"}
        )

    assert resp.status_code == 422
    mocks["setup_service"].create_root_once.assert_not_awaited()


async def test_setup_service_validation_error_returns_422() -> None:
    """Schema-valid but weak password → service ValidationError → 422."""
    app, mocks = _create_app()
    mocks["setup_service"].create_root_once.side_effect = ValidationError(
        "Password must contain at least one uppercase letter."
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/auth/setup", json={"password": "weakpass1"}
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "Password must contain at least one uppercase letter."
    )
