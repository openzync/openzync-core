"""Unit tests for the /v1/users router — HTTP adapter layer only.

Covers all 11 endpoints: CRUD, user summary, and custom-instructions
management.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from schemas.users import (
    UserResponse,
    UserResponseWithStats,
    UserListResponse,
)
from schemas.user_summary import UserSummaryResponse, UserSummaryTriggerResponse
from schemas.custom_instructions import CustomInstructionsResponse, CustomInstructionSchema

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


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


def _make_user_response(overrides: dict | None = None) -> dict:
    """Build a realistic UserResponse dict."""
    base = {
        "id": USER_ID,
        "external_id": "user_abc123",
        "name": "Alice Johnson",
        "email": "alice@example.com",
        "metadata": {},
        "organization_id": ORG_ID,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "is_deleted": False,
    }
    base.update(overrides or {})
    return base


class TestUsersRouter:
    """Full HTTP-adapter tests for the users router."""

    # ── POST /v1/users ──────────────────────────────────────────────────

    @patch("routers.users.UserService")
    async def test_create_user_success(self, mock_user_service: AsyncMock) -> None:
        """POST /v1/users → 201 with UserResponse."""
        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance
        mock_instance.create_user.return_value = UserResponse.model_validate(
            _make_user_response(),
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_admin, require_org_id
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

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
                "/v1/users",
                json={
                    "external_id": "user_abc123",
                    "name": "Alice Johnson",
                    "email": "alice@example.com",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["external_id"] == "user_abc123"
        assert data["name"] == "Alice Johnson"
        mock_instance.create_user.assert_awaited_once()

    @patch("routers.users.UserService")
    async def test_create_user_422_missing_external_id(
        self, mock_user_service: AsyncMock,
    ) -> None:
        """POST /v1/users without external_id → 422."""
        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance

        from dependencies.db import get_db
        from dependencies.auth import require_org_admin, require_org_id
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/users", json={})

        assert response.status_code == 422
        mock_instance.create_user.assert_not_called()

    @patch("routers.users.UserService")
    async def test_create_user_422_invalid_email(
        self, mock_user_service: AsyncMock,
    ) -> None:
        """POST /v1/users with invalid email → 422."""
        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance

        from dependencies.db import get_db
        from dependencies.auth import require_org_admin, require_org_id
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

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
                "/v1/users",
                json={
                    "external_id": "u1",
                    "email": "not-an-email",
                },
            )

        assert response.status_code == 422

    # ── GET /v1/users ───────────────────────────────────────────────────

    @patch("routers.users.UserService")
    async def test_list_users_success(self, mock_user_service: AsyncMock) -> None:
        """GET /v1/users → 200 with UserListResponse."""
        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance
        mock_instance.list_users.return_value = UserListResponse(
            data=[UserResponse.model_validate(_make_user_response())],
            next_cursor=None,
            has_more=False,
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_admin, require_org_id
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/users")

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert len(data["data"]) == 1
        mock_instance.list_users.assert_awaited_once()

    # ── GET /v1/users/{user_id} ──────────────────────────────────────────

    @patch("routers.users.UserService")
    async def test_get_user_success(self, mock_user_service: AsyncMock) -> None:
        """GET /v1/users/{id} → 200 with UserResponseWithStats."""
        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance
        mock_instance.get_user.return_value = UserResponseWithStats.model_validate(
            _make_user_response({
                "message_count": 10,
                "fact_count": 5,
                "session_count": 3,
            }),
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_admin, require_org_id
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/users/{USER_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(USER_ID)
        assert data["message_count"] == 10
        mock_instance.get_user.assert_awaited_once()

    @patch("routers.users.UserService")
    async def test_get_user_404_not_found(self, mock_user_service: AsyncMock) -> None:
        """GET /v1/users/{id} when not found → 404."""
        from core.exceptions import NotFoundError, register_exception_handlers

        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance
        mock_instance.get_user.side_effect = NotFoundError(
            message="User not found",
            detail={"user_id": str(USER_ID)},
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_id
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        register_exception_handlers(app)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/users/{USER_ID}")

        assert response.status_code == 404

    # ── PATCH /v1/users/{user_id} ───────────────────────────────────────

    @patch("routers.users.UserService")
    async def test_update_user_success(self, mock_user_service: AsyncMock) -> None:
        """PATCH /v1/users/{id} → 200 with updated UserResponse."""
        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance
        mock_instance.update_user.return_value = UserResponse.model_validate(
            _make_user_response({"name": "Alice B."}),
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_admin, require_org_id
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.patch(
                f"/v1/users/{USER_ID}",
                json={"name": "Alice B."},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Alice B."
        mock_instance.update_user.assert_awaited_once()

    # ── DELETE /v1/users/{user_id} ──────────────────────────────────────

    @patch("routers.users.UserService")
    async def test_delete_user_success(self, mock_user_service: AsyncMock) -> None:
        """DELETE /v1/users/{id} → 204 No Content."""
        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance
        mock_instance.delete_user.return_value = None

        from dependencies.db import get_db
        from dependencies.auth import require_org_admin, require_org_id
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(f"/v1/users/{USER_ID}")

        assert response.status_code == 204
        assert response.content == b""
        mock_instance.delete_user.assert_awaited_once()

    # ── GET /v1/users/{user_id}/summary ─────────────────────────────────

    @patch("routers.users.UserSummaryService")
    async def test_get_user_summary_success(
        self, mock_summary_service: AsyncMock,
    ) -> None:
        """GET /v1/users/{id}/summary → 200 with UserSummaryResponse."""
        mock_instance = AsyncMock()
        mock_summary_service.return_value = mock_instance
        mock_instance.get_summary.return_value = UserSummaryResponse(
            user_id=USER_ID,
            summary="Alice is a helpful user.",
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_id
        from core.arq import get_arq
        from routers.users import get_user_summary_service, router

        # get_user_summary_service needs arq and redis on app.state
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_summary_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        # We need to ensure that core.arq.get_arq exists during import
        try:
            from core.arq import get_arq
            _has_arq = True
        except (ImportError, AttributeError):
            _has_arq = False

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/users/{USER_ID}/summary")

        assert response.status_code == 200
        data = response.json()
        assert data["summary"] == "Alice is a helpful user."
        mock_instance.get_summary.assert_awaited_once()

    @patch("routers.users.UserSummaryService")
    async def test_get_user_summary_404_not_found(
        self, mock_summary_service: AsyncMock,
    ) -> None:
        """GET /v1/users/{id}/summary when no summary exists → 404."""
        mock_instance = AsyncMock()
        mock_summary_service.return_value = mock_instance
        mock_instance.get_summary.return_value = None

        from dependencies.db import get_db
        from dependencies.auth import require_org_id
        from routers.users import get_user_summary_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_summary_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/users/{USER_ID}/summary")

        assert response.status_code == 404
        mock_instance.get_summary.assert_awaited_once()

    # ── POST /v1/users/{user_id}/summary ────────────────────────────────

    @patch("routers.users.UserSummaryService")
    async def test_trigger_user_summary_success(
        self, mock_summary_service: AsyncMock,
    ) -> None:
        """POST /v1/users/{id}/summary → 202 with UserSummaryTriggerResponse."""
        mock_instance = AsyncMock()
        mock_summary_service.return_value = mock_instance
        mock_instance.trigger_generation.return_value = UserSummaryTriggerResponse(
            message="Summary generation started.",
            status="processing",
            user_id=USER_ID,
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_id
        from routers.users import get_user_summary_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_summary_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(f"/v1/users/{USER_ID}/summary")

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "processing"
        mock_instance.trigger_generation.assert_awaited_once()

    @patch("routers.users.UserSummaryService")
    async def test_trigger_user_summary_rate_limited(
        self, mock_summary_service: AsyncMock,
    ) -> None:
        """POST /v1/users/{id}/summary when rate-limited → 429."""
        from core.exceptions import RateLimitError, register_exception_handlers

        mock_instance = AsyncMock()
        mock_summary_service.return_value = mock_instance
        mock_instance.trigger_generation.side_effect = RateLimitError(
            message="Rate limited. Try again in 5 minutes.",
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_id
        from routers.users import get_user_summary_service, router

        app = FastAPI()
        app.include_router(router)
        register_exception_handlers(app)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_summary_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(f"/v1/users/{USER_ID}/summary")

        assert response.status_code == 429
        mock_instance.trigger_generation.assert_awaited_once()

    # ── GET /v1/users/{user_id}/summary-instructions ─────────────────────

    @patch("routers.users.UserSummaryService")
    async def test_list_summary_instructions_success(
        self, mock_summary_service: AsyncMock,
    ) -> None:
        """GET /v1/users/{id}/summary-instructions → 200 with instructions."""
        mock_instance = AsyncMock()
        mock_summary_service.return_value = mock_instance
        mock_instance.get_instructions.return_value = [
            {"name": "legal_domain", "text": "Focus on legal terms."},
        ]

        from dependencies.db import get_db
        from dependencies.auth import require_org_id
        from routers.users import get_user_summary_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_summary_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

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
                f"/v1/users/{USER_ID}/summary-instructions",
            )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert len(data["data"]) == 1
        assert data["data"][0]["name"] == "legal_domain"
        mock_instance.get_instructions.assert_awaited_once()

    # ── PUT /v1/users/{user_id}/summary-instructions ─────────────────────

    @patch("routers.users.UserSummaryService")
    async def test_set_summary_instructions_success(
        self, mock_summary_service: AsyncMock,
    ) -> None:
        """PUT /v1/users/{id}/summary-instructions → 201."""
        mock_instance = AsyncMock()
        mock_summary_service.return_value = mock_instance
        mock_instance.set_instructions.return_value = [
            {"name": "healthcare", "text": "Focus on medical terms."},
        ]

        from dependencies.db import get_db
        from dependencies.auth import require_org_id
        from routers.users import get_user_summary_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_summary_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                f"/v1/users/{USER_ID}/summary-instructions",
                json={
                    "instructions": [
                        {"name": "healthcare", "text": "Focus on medical terms."},
                    ],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["name"] == "healthcare"
        mock_instance.set_instructions.assert_awaited_once()

    # ── DELETE /v1/users/{user_id}/summary-instructions ─────────────────

    @patch("routers.users.UserSummaryService")
    async def test_delete_summary_instructions_success(
        self, mock_summary_service: AsyncMock,
    ) -> None:
        """DELETE /v1/users/{id}/summary-instructions → 204."""
        mock_instance = AsyncMock()
        mock_summary_service.return_value = mock_instance
        mock_instance.delete_instructions.return_value = None

        from dependencies.db import get_db
        from dependencies.auth import require_org_id
        from routers.users import get_user_summary_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_summary_service] = lambda: mock_instance
        app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)

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
                f"/v1/users/{USER_ID}/summary-instructions",
            )

        assert response.status_code == 204
        assert response.content == b""
        mock_instance.delete_instructions.assert_awaited_once()


class TestUsersRouterRoleChanges:
    """PATCH /v1/users/{id} with ``role`` — admin-gated role management.

    Observed contract:
    - Admin (JWT + org admin role) → 200, role change passes to the service.
    - Member (JWT + member role) → 403 (require_org_admin).
    - API-key auth attempting a role change → 403 (router-level guard).
    """

    @patch("routers.users.UserService")
    async def test_update_role_admin_200(
        self, mock_user_service: AsyncMock,
    ) -> None:
        """PATCH with ``role`` as an org admin → 200 with updated role."""
        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance
        mock_instance.update_user.return_value = UserResponse.model_validate(
            _make_user_response({"role": "admin", "external_id": "u_1"}),
        )

        from dependencies.db import get_db
        from dependencies.auth import require_org_admin
        from routers.users import get_user_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.patch(
                f"/v1/users/{UUID('00000000-0000-0000-0000-0000000000bb')}",
                json={"role": "admin"},
            )

        assert response.status_code == 200
        assert response.json()["role"] == "admin"
        # The service receives the role change with the actor's user id.
        mock_instance.update_user.assert_awaited_once_with(
            organization_id=ORG_ID,
            user_id=UUID("00000000-0000-0000-0000-0000000000bb"),
            update_fields={"role": "admin"},
            actor_user_id=USER_ID,
        )

    @patch("routers.users.UserService")
    async def test_update_role_member_403(
        self, mock_user_service: AsyncMock,
    ) -> None:
        """PATCH with ``role`` as a JWT member → 403 (real role check)."""
        from dependencies.db import get_db
        from routers.users import get_user_service, router

        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance

        app = FastAPI()
        app.state.redis = AsyncMock()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_user_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        app.include_router(router)

        from dependencies.auth import get_org_role

        with patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="member"),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.patch(
                    f"/v1/users/{USER_ID}",
                    json={"role": "admin"},
                )

        assert response.status_code == 403
        mock_instance.update_user.assert_not_awaited()

    @patch("routers.users.UserService")
    async def test_update_role_api_key_403(
        self, mock_user_service: AsyncMock,
    ) -> None:
        """API-key auth with ``role`` in the payload → 403 (router guard).

        API keys can never change roles — only a JWT dashboard session can.
        """
        from dependencies.db import get_db
        from dependencies.auth import require_org_admin
        from routers.users import get_user_service, router

        mock_instance = AsyncMock()
        mock_user_service.return_value = mock_instance

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[get_user_service] = lambda: mock_instance
        app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "api_key"
            request.state.api_key_scopes = ["admin", "admin:write"]
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.patch(
                f"/v1/users/{USER_ID}",
                json={"role": "admin"},
            )

        assert response.status_code == 403
        assert "API keys cannot change user roles" in response.json()["detail"]
        mock_instance.update_user.assert_not_awaited()
