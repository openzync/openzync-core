"""Unit tests for the public invite endpoints on the auth router.

Tests ``POST /v1/auth/invites/info`` and ``POST /v1/auth/invites/accept``:
- 200 for valid tokens (info → invite details; accept → JWT pair).
- Generic 404 for unknown/expired/used tokens.
- The token travels in the POST body — never the URL path (a path token
  would be a live bearer credential in request logs).
- Both endpoints are PUBLIC — no auth dependency at all.

"""

# ruff: noqa: S105, S106  — every fixture here IS a token/password by design

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.exceptions import NotFoundError, register_exception_handlers
from dependencies.services import get_invite_service
from routers.auth import router
from schemas.auth import InviteInfoResponse, TokenResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


def _create_app() -> tuple[FastAPI, AsyncMock]:
    """Minimal app with the auth router and the invite service mocked.

    Note: no auth middleware and no throttle override — these two
    endpoints must not declare any auth/throttle dependency (public).
    """
    app = FastAPI()
    register_exception_handlers(app)
    service_mock = AsyncMock()
    app.dependency_overrides[get_invite_service] = lambda: service_mock
    app.include_router(router)
    return app, service_mock


# ── POST /v1/auth/invites/info ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_info_200() -> None:
    """Valid token → 200 with org name + invitee identity."""
    app, service_mock = _create_app()
    service_mock.get_invite_info.return_value = InviteInfoResponse(
        org_name="Acme Corp",
        email="alice@acme.com",
        name="Alice Johnson",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/invites/info",
            json={"token": "raw-token"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "org_name": "Acme Corp",
        "email": "alice@acme.com",
        "name": "Alice Johnson",
    }
    service_mock.get_invite_info.assert_awaited_once_with("raw-token")


@pytest.mark.asyncio
async def test_invite_info_generic_404() -> None:
    """Unknown/expired/used token → generic 404."""
    app, service_mock = _create_app()
    service_mock.get_invite_info.side_effect = NotFoundError(
        "This invitation link is invalid or has expired."
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/invites/info",
            json={"token": "raw-token"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "This invitation link is invalid or has expired."


@pytest.mark.asyncio
async def test_invite_info_empty_token_422() -> None:
    """Missing/empty token body → 422."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/auth/invites/info", json={})

    assert resp.status_code == 422


# ── POST /v1/auth/invites/accept ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_200_token_pair() -> None:
    """Valid token + password → 200 with a JWT pair (invitee logged in)."""
    app, service_mock = _create_app()
    service_mock.accept_invite.return_value = TokenResponse(
        access_token="access.jwt.token",
        refresh_token="raw-refresh-token",
        expires_in=1800,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/invites/accept",
            json={"token": "raw-token", "password": "SecurePass1"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "access.jwt.token"
    assert body["refresh_token"] == "raw-refresh-token"
    assert body["expires_in"] == 1800
    assert body["token_type"] == "Bearer"
    service_mock.accept_invite.assert_awaited_once_with(
        token="raw-token",
        password="SecurePass1",
    )


@pytest.mark.asyncio
async def test_accept_generic_404() -> None:
    """Used/expired/unknown token → generic 404 (replay safe)."""
    app, service_mock = _create_app()
    service_mock.accept_invite.side_effect = NotFoundError(
        "This invitation link is invalid or has expired."
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/invites/accept",
            json={"token": "raw-token", "password": "SecurePass1"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "This invitation link is invalid or has expired."


@pytest.mark.asyncio
async def test_accept_weak_password_422() -> None:
    """Too-weak password → 422 (schema-level min_length)."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/invites/accept",
            json={"token": "raw-token", "password": "short"},
        )

    assert resp.status_code == 422


# ── Token in body, never in path ───────────────────────────────────────────────


def test_invite_route_paths_have_no_token_param() -> None:
    """Both invite routes take the token from the body, not the path.

    A path-based token would be persisted verbatim by LoggingMiddleware
    (INFO-level request logs) — a live bearer credential in logs.
    """
    paths = {route.path for route in router.routes if "invite" in route.path}
    assert paths == {
        "/v1/auth/invites/info",
        "/v1/auth/invites/accept",
    }
    for route in router.routes:
        if "invite" in route.path:
            assert "{token}" not in route.path
