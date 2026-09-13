"""Unit tests for the API key self-service router.

Tests the endpoint under ``/v1/api-key``:
- ``GET /project-id`` — returns the project_id bound to the API key

The endpoint is minimal: it reads ``request.state.api_key_project_id`` and
returns ``{"project_id": ...}``.  For JWT-authenticated requests this field
is ``None``.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from routers.api_key_self import router

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


def _build_app(state_overrides: dict | None = None) -> FastAPI:
    """Create a minimal FastAPI app with the api-key-self router.

    Args:
        state_overrides: Additional ``request.state`` attributes to set.
    """
    app = FastAPI()
    app.include_router(router)

    from dependencies.auth import require_org_id

    merged_state = {
        "org_id": str(ORG_ID),
        "auth_type": "api_key",
        "api_key_project_id": str(PROJECT_ID),
        "api_key_permissions": ["read", "write"],
    }
    if state_overrides:
        merged_state.update(state_overrides)

    # Only override require_org_id when org_id is set — otherwise let the
    # real dependency run and raise 401 for the "without auth" test case.
    app.dependency_overrides = {}
    if merged_state.get("org_id"):
        app.dependency_overrides[require_org_id] = lambda: merged_state["org_id"]

    @app.middleware("http")
    async def _mock_auth(request: Request, call_next):
        for key, value in merged_state.items():
            setattr(request.state, key, value)
        response = await call_next(request)
        return response

    return app


@pytest.fixture
async def client() -> AsyncClient:  # noqa: ANN201
    """Default client with API-key-authenticated state (has project_id)."""
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def jwt_client() -> AsyncClient:  # noqa: ANN201
    """Client with JWT-authenticated state (no api_key_project_id)."""
    app = _build_app(
        state_overrides={
            "auth_type": "jwt",
            "api_key_project_id": None,
            "user_id": str(UUID("00000000-0000-0000-0000-000000000002")),
        }
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── GET /project-id — returns project_id for API-key auth ────────────────────


class TestGetApiKeyProjectId:
    """GET /v1/api-key/project-id — resolve project context."""

    async def test_returns_project_id_for_api_key(
        self, client: AsyncClient
    ) -> None:
        """Should return the project_id when request is API-key-authenticated."""
        response = await client.get("/v1/api-key/project-id")
        assert response.status_code == 200
        body = response.json()
        assert body["project_id"] == str(PROJECT_ID)

    async def test_returns_null_for_jwt(
        self, jwt_client: AsyncClient
    ) -> None:
        """Should return null project_id for JWT-authenticated dashboard users."""
        response = await jwt_client.get("/v1/api-key/project-id")
        assert response.status_code == 200
        body = response.json()
        assert body["project_id"] is None

    async def test_returns_401_without_auth(self) -> None:
        """Should return 401 when no auth is provided (require_org_id raises)."""
        app = _build_app(
            state_overrides={
                "org_id": None,
                "auth_type": None,
                "api_key_project_id": None,
            }
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            response = await ac.get("/v1/api-key/project-id")
        assert response.status_code == 401
