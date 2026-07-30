"""Unit tests for the /v1/projects/{project_id}/api-keys router — HTTP adapter layer.

Covers all 3 endpoints: list, create, and revoke (soft-delete) project-scoped
API keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from schemas.api_keys import (
    ApiKeyListResponse,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    CreateApiKeyRequest,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
KEY_ID = UUID("00000000-0000-0000-0000-000000000010")


@pytest.fixture(autouse=True)
def _init_settings() -> None:
    """Initialise the Settings singleton with dummy values."""
    from core.config import Settings, set_settings

    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
        REDIS_URL="redis://localhost:6379/1",
        SECRET_KEY="a" * 32,
        WEBHOOK_SIGNING_SECRET="b" * 32,
        ENVIRONMENT="test",
    )
    set_settings(settings)


def _make_api_key_response(overrides: dict | None = None) -> dict:
    """Build a realistic ApiKeyResponse dict."""
    base = {
        "id": KEY_ID,
        "name": "Production Key",
        "prefix": "oz_live_",
        "project_id": PROJECT_ID,
        "created_by": USER_ID,
        "scopes": ["read", "write"],
        "is_revoked": False,
        "last_used_at": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "raw_key": None,
    }
    base.update(overrides or {})
    return base


class TestProjectApiKeysRouter:
    """Full HTTP-adapter tests for the project API keys router."""

    # ── GET /v1/projects/{project_id}/api-keys ──────────────────────────

    @patch("routers.project_api_keys.ApiKeyService")
    async def test_list_api_keys_success(self, mock_api_key_service: AsyncMock) -> None:
        """GET .../api-keys → 200 with ApiKeyListResponse."""
        mock_instance = AsyncMock()
        mock_api_key_service.return_value = mock_instance
        mock_instance.list_project_keys.return_value = [
            _make_api_key_response(),
        ]

        from dependencies.db import get_db
        from core.redis import get_redis
        from dependencies.auth import require_org_id, get_current_user_id
        from dependencies.project_auth import require_project_owner
        from routers.project_api_keys import _get_service, router

        app = FastAPI()
        app.include_router(router)

        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        app.dependency_overrides[require_project_owner] = lambda: None
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[_get_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/api-keys",
            )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert "total" in data
        assert len(data["data"]) == 1
        assert data["data"][0]["name"] == "Production Key"
        mock_instance.list_project_keys.assert_awaited_once()

    @patch("routers.project_api_keys.ApiKeyService")
    async def test_list_api_keys_empty(self, mock_api_key_service: AsyncMock) -> None:
        """GET .../api-keys when none exist → 200 with empty list."""
        mock_instance = AsyncMock()
        mock_api_key_service.return_value = mock_instance
        mock_instance.list_project_keys.return_value = []

        from dependencies.db import get_db
        from core.redis import get_redis
        from dependencies.auth import require_org_id, get_current_user_id
        from dependencies.project_auth import require_project_owner
        from routers.project_api_keys import _get_service, router

        app = FastAPI()
        app.include_router(router)

        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        app.dependency_overrides[require_project_owner] = lambda: None
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[_get_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/api-keys",
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"] == []
        assert data["total"] == 0
        mock_instance.list_project_keys.assert_awaited_once()

    # ── POST /v1/projects/{project_id}/api-keys ─────────────────────────

    @patch("routers.project_api_keys.ApiKeyService")
    async def test_create_api_key_success(self, mock_api_key_service: AsyncMock) -> None:
        """POST .../api-keys → 201 with ApiKeyCreatedResponse including raw_key."""
        mock_instance = AsyncMock()
        mock_api_key_service.return_value = mock_instance
        mock_api_key = ApiKeyResponse.model_validate(_make_api_key_response())
        mock_instance.create_project_key.return_value = (
            mock_api_key,
            "oz_live_abc123def456",
        )

        from dependencies.db import get_db
        from core.redis import get_redis
        from dependencies.auth import require_org_id, get_current_user_id
        from dependencies.project_auth import require_project_owner
        from routers.project_api_keys import _get_service, router

        app = FastAPI()
        app.include_router(router)

        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        app.dependency_overrides[require_project_owner] = lambda: None
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[_get_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/projects/{PROJECT_ID}/api-keys",
                json={"name": "Production Key"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Production Key"
        assert data["raw_key"] == "oz_live_abc123def456"
        assert data["prefix"] == "oz_live_"
        assert "message" in data
        mock_instance.create_project_key.assert_awaited_once()

    @patch("routers.project_api_keys.ApiKeyService")
    async def test_create_api_key_422_empty_name(
        self, mock_api_key_service: AsyncMock,
    ) -> None:
        """POST .../api-keys with empty name → 422."""
        mock_instance = AsyncMock()
        mock_api_key_service.return_value = mock_instance

        from dependencies.db import get_db
        from core.redis import get_redis
        from dependencies.auth import require_org_id, get_current_user_id
        from dependencies.project_auth import require_project_owner
        from routers.project_api_keys import _get_service, router

        app = FastAPI()
        app.include_router(router)

        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        app.dependency_overrides[require_project_owner] = lambda: None
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[_get_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/projects/{PROJECT_ID}/api-keys",
                json={"name": ""},
            )

        assert response.status_code == 422
        mock_instance.create_project_key.assert_not_called()

    # ── DELETE /v1/projects/{project_id}/api-keys/{key_id} ──────────────

    @patch("routers.project_api_keys.ApiKeyService")
    async def test_revoke_api_key_success(self, mock_api_key_service: AsyncMock) -> None:
        """DELETE .../api-keys/{key_id} → 204 No Content."""
        mock_instance = AsyncMock()
        mock_api_key_service.return_value = mock_instance
        mock_instance.revoke_project_key.return_value = _make_api_key_response(
            {"is_revoked": True},
        )

        from dependencies.db import get_db
        from core.redis import get_redis
        from dependencies.auth import require_org_id, get_current_user_id
        from dependencies.project_auth import require_project_owner
        from routers.project_api_keys import _get_service, router

        app = FastAPI()
        app.include_router(router)

        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        app.dependency_overrides[require_project_owner] = lambda: None
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[_get_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(
                f"/v1/projects/{PROJECT_ID}/api-keys/{KEY_ID}",
            )

        assert response.status_code == 204
        assert response.content == b""
        mock_instance.revoke_project_key.assert_awaited_once()

    @patch("routers.project_api_keys.ApiKeyService")
    async def test_revoke_api_key_404_not_found(
        self, mock_api_key_service: AsyncMock,
    ) -> None:
        """DELETE .../api-keys/{key_id} when not found → 404."""
        mock_instance = AsyncMock()
        mock_api_key_service.return_value = mock_instance
        mock_instance.revoke_project_key.return_value = None

        from dependencies.db import get_db
        from core.redis import get_redis
        from dependencies.auth import require_org_id, get_current_user_id
        from dependencies.project_auth import require_project_owner
        from routers.project_api_keys import _get_service, router

        app = FastAPI()
        app.include_router(router)

        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        app.dependency_overrides[require_project_owner] = lambda: None
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[_get_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(
                f"/v1/projects/{PROJECT_ID}/api-keys/{KEY_ID}",
            )

        assert response.status_code == 404
        mock_instance.revoke_project_key.assert_awaited_once()
