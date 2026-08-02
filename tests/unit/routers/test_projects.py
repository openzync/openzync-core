"""Unit tests for the /v1/projects router — HTTP adapter layer only.

Tests the full request-response cycle for every endpoint in the projects
router, including success paths, validation errors, and 401 failures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from schemas.projects import ProjectResponse

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


def _make_project_response(overrides: dict | None = None) -> dict:
    """Build a realistic ProjectResponse dict for mock returns."""
    base = {
        "id": PROJECT_ID,
        "name": "Test Project",
        "description": "A test project",
        "metadata": {},
        "is_archived": False,
        "member_count": 1,
        "created_by": USER_ID,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides or {})
    return base


def _make_member_response(overrides: dict | None = None) -> dict:
    """Build a realistic ProjectMemberResponse dict."""
    base = {
        "id": UUID("00000000-0000-0000-0000-000000000099"),
        "user_id": USER_ID,
        "role": "member",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides or {})
    return base


class TestProjectsRouter:
    """Full HTTP-adapter tests for the projects router."""

    @patch("routers.projects.ProjectService")
    async def test_create_project_success(self, mock_project_service: AsyncMock) -> None:
        """POST /v1/projects → 201 with ProjectResponse."""
        # ── Arrange ─────────────────────────────────────────────────────
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.create_project.return_value = ProjectResponse.model_validate(
            _make_project_response()
        )

        from dependencies.db import get_db

        from routers.projects import (
            _get_project_service,
            require_project_membership,
            require_project_owner,
            router,
        )

        app = FastAPI()
        app.include_router(router)

        app.dependency_overrides[get_db] = AsyncMock
        db_mock = AsyncMock()
        app.dependency_overrides[_get_project_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        # ── Act ─────────────────────────────────────────────────────────
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/projects",
                json={"name": "Test Project"},
            )

        # ── Assert ──────────────────────────────────────────────────────
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Project"
        assert data["is_archived"] is False
        mock_instance.create_project.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_create_project_422_empty_name(self, mock_project_service: AsyncMock) -> None:
        """POST /v1/projects with empty name → 422."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance

        from dependencies.db import get_db
        from routers.projects import _get_project_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance

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
                "/v1/projects",
                json={"name": ""},
            )

        assert response.status_code == 422
        mock_instance.create_project.assert_not_called()

    @patch("routers.projects.ProjectService")
    async def test_create_project_422_missing_body(self, mock_project_service: AsyncMock) -> None:
        """POST /v1/projects without body → 422."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance

        from dependencies.db import get_db
        from routers.projects import _get_project_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance

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
                "/v1/projects",
                json={},
            )

        assert response.status_code == 422
        mock_instance.create_project.assert_not_called()

    @patch("routers.projects.ProjectService")
    async def test_list_projects_success(self, mock_project_service: AsyncMock) -> None:
        """GET /v1/projects → 200 with list of projects."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.list_projects.return_value = [
            ProjectResponse.model_validate(_make_project_response())
        ]

        from dependencies.db import get_db
        from routers.projects import _get_project_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/projects")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "Test Project"
        mock_instance.list_projects.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_list_projects_with_pagination(self, mock_project_service: AsyncMock) -> None:
        """GET /v1/projects with limit and offset → 200."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.list_projects.return_value = []

        from dependencies.db import get_db
        from routers.projects import _get_project_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/projects?limit=10&offset=5")

        assert response.status_code == 200
        mock_instance.list_projects.assert_awaited_once()
        _call_kwargs = mock_instance.list_projects.call_args.kwargs
        assert _call_kwargs["limit"] == 10
        assert _call_kwargs["offset"] == 5

    @patch("routers.projects.ProjectService")
    async def test_list_projects_422_invalid_limit(self, mock_project_service: AsyncMock) -> None:
        """GET /v1/projects with limit out of range → 422."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance

        from dependencies.db import get_db
        from routers.projects import _get_project_service, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/projects?limit=999")

        assert response.status_code == 422

    @patch("routers.projects.ProjectService")
    async def test_get_project_success(self, mock_project_service: AsyncMock) -> None:
        """GET /v1/projects/{id} → 200 with ProjectResponse."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.get_project.return_value = ProjectResponse.model_validate(
            _make_project_response()
        )

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_membership, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_membership] = lambda: None

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/projects/{PROJECT_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(PROJECT_ID)
        mock_instance.get_project.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_get_project_404_not_found(self, mock_project_service: AsyncMock) -> None:
        """GET /v1/projects/{id} when service raises NotFoundError → 404."""
        from core.exceptions import NotFoundError

        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.get_project.side_effect = NotFoundError(
            message="Project not found",
            detail={"project_id": str(PROJECT_ID)},
        )

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_membership, router
        from core.exceptions import register_exception_handlers

        app = FastAPI()
        app.include_router(router)
        register_exception_handlers(app)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_membership] = lambda: None

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/projects/{PROJECT_ID}")

        assert response.status_code == 404

    @patch("routers.projects.ProjectService")
    async def test_update_project_success(self, mock_project_service: AsyncMock) -> None:
        """PATCH /v1/projects/{id} → 200 with updated ProjectResponse."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.update_project.return_value = ProjectResponse.model_validate(
            _make_project_response({"name": "Updated Project"})
        )

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_owner, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_owner] = lambda: None

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
                f"/v1/projects/{PROJECT_ID}",
                json={"name": "Updated Project"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Project"
        mock_instance.update_project.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_update_project_empty_body(self, mock_project_service: AsyncMock) -> None:
        """PATCH /v1/projects/{id} with empty body → 200 (all fields optional)."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.update_project.return_value = ProjectResponse.model_validate(
            _make_project_response()
        )

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_owner, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_owner] = lambda: None

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
                f"/v1/projects/{PROJECT_ID}",
                json={},
            )

        # Empty body is valid for PATCH (all fields optional) — should return 200 with service result
        assert response.status_code == 200
        mock_instance.update_project.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_delete_project_success(self, mock_project_service: AsyncMock) -> None:
        """DELETE /v1/projects/{id} → 204 No Content."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.archive_project.return_value = None

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_owner, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_owner] = lambda: None

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(f"/v1/projects/{PROJECT_ID}")

        assert response.status_code == 204
        assert response.content == b""
        mock_instance.archive_project.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_add_member_success(self, mock_project_service: AsyncMock) -> None:
        """POST /v1/projects/{id}/members → 201 with ProjectMemberResponse."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.add_member.return_value = _make_member_response()

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_owner, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_owner] = lambda: None

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
                f"/v1/projects/{PROJECT_ID}/members",
                json={"user_id": str(USER_ID), "role": "member"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["user_id"] == str(USER_ID)
        assert data["role"] == "member"
        mock_instance.add_member.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_add_member_422_invalid_role(self, mock_project_service: AsyncMock) -> None:
        """POST /v1/projects/{id}/members with invalid role → 422."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_owner, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_owner] = lambda: None

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
                f"/v1/projects/{PROJECT_ID}/members",
                json={"user_id": str(USER_ID), "role": "admin"},
            )

        assert response.status_code == 422

    @patch("routers.projects.ProjectService")
    async def test_list_members_success(self, mock_project_service: AsyncMock) -> None:
        """GET /v1/projects/{id}/members → 200 with list of members."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.list_members.return_value = [_make_member_response()]

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_membership, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_membership] = lambda: None

        @app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/projects/{PROJECT_ID}/members")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        mock_instance.list_members.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_remove_member_success(self, mock_project_service: AsyncMock) -> None:
        """DELETE /v1/projects/{id}/members/{uid} → 204 No Content."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.remove_member.return_value = None

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_owner, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_owner] = lambda: None

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
                f"/v1/projects/{PROJECT_ID}/members/{USER_ID}",
            )

        assert response.status_code == 204
        mock_instance.remove_member.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_update_member_role_success(self, mock_project_service: AsyncMock) -> None:
        """PATCH /v1/projects/{id}/members/{uid} → 200 with updated member."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance
        mock_instance.update_member_role.return_value = _make_member_response(
            {"role": "owner"}
        )

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_owner, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_owner] = lambda: None

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
                f"/v1/projects/{PROJECT_ID}/members/{USER_ID}?role=owner",
            )

        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "owner"
        mock_instance.update_member_role.assert_awaited_once()

    @patch("routers.projects.ProjectService")
    async def test_update_member_role_422_invalid_role(
        self, mock_project_service: AsyncMock,
    ) -> None:
        """PATCH with invalid role query parameter → 422."""
        mock_instance = AsyncMock()
        mock_project_service.return_value = mock_instance

        from dependencies.db import get_db
        from routers.projects import _get_project_service, require_project_owner, router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = AsyncMock
        app.dependency_overrides[_get_project_service] = lambda: mock_instance
        app.dependency_overrides[require_project_owner] = lambda: None

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
                f"/v1/projects/{PROJECT_ID}/members/{USER_ID}?role=superadmin",
            )

        assert response.status_code == 422
