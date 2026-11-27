"""Unit tests for dependencies/project_auth.py — project-scoped auth checks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from models.project import Project
from models.project_member import ProjectMember

pytestmark = pytest.mark.unit

PROJECT_ID = UUID("00000000-0000-0000-0000-0000000000aa")
ORG_ID_STR = "00000000-0000-0000-0000-000000000001"
USER_ID_STR = "00000000-0000-0000-0000-000000000002"


def _make_project() -> Project:
    """Real Project entity — JWT branch returns it from get_accessible_project."""
    return Project(
        id=PROJECT_ID,
        user_id=UUID(ORG_ID_STR),
        name="Test Project",
        created_by=UUID(USER_ID_STR),
        is_archived=False,
    )


def _make_member() -> ProjectMember:
    """Real active ProjectMember entity."""
    return ProjectMember(
        project_id=PROJECT_ID,
        user_id=UUID(USER_ID_STR),
        role="member",
        status="active",
    )


class TestRequireProjectMembership:
    """require_project_membership: unified JWT/API-key project auth."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def mock_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.get_by_id = AsyncMock()
        repo.get_accessible_project = AsyncMock()
        repo.get_active_member = AsyncMock()
        return repo

    @pytest.mark.asyncio
    async def test_jwt_valid_member_passes(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """JWT user who is a project member → passes (returns None)."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_accessible_project.return_value = _make_project()
        mock_repo.get_active_member.return_value = _make_member()

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ):
            result = await require_project_membership(request, PROJECT_ID, db_session)

        assert result is None
        mock_repo.get_accessible_project.assert_awaited_once_with(
            project_id=PROJECT_ID, user_id=UUID(USER_ID_STR)
        )
        mock_repo.get_active_member.assert_awaited_once_with(
            project_id=PROJECT_ID, user_id=UUID(USER_ID_STR)
        )

    @pytest.mark.asyncio
    async def test_jwt_non_member_raises_403(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """JWT user who is NOT a member → raises 403."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_accessible_project.return_value = _make_project()
        mock_repo.get_active_member.return_value = None  # not a member

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ), pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)

        assert exc.value.status_code == 403
        assert "Not a member" in exc.value.detail

    @pytest.mark.asyncio
    async def test_jwt_project_not_found_raises_404(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """Project does not exist → raises 404."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_accessible_project.return_value = None  # not found
        mock_repo.get_active_member = AsyncMock()

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ), pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)

        assert exc.value.status_code == 404
        assert "Project not found" in exc.value.detail

    @pytest.mark.asyncio
    async def test_jwt_missing_user_id_raises_401(
        self, db_session: AsyncMock,
    ) -> None:
        """JWT without user_id → raises 401."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.user_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_user_id_raises_401(
        self, db_session: AsyncMock,
    ) -> None:
        """Missing user_id → raises 401."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_api_key_scoped_to_correct_project_passes(
        self, db_session: AsyncMock,
    ) -> None:
        """API key scoped to the requested project → passes."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.user_id = ORG_ID_STR
        request.state.auth_type = "api_key"
        request.state.api_key_project_id = str(PROJECT_ID)

        result = await require_project_membership(request, PROJECT_ID, db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_api_key_wrong_project_raises_403(
        self, db_session: AsyncMock,
    ) -> None:
        """API key scoped to a different project → raises 403."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.user_id = ORG_ID_STR
        request.state.auth_type = "api_key"
        request.state.api_key_project_id = "00000000-0000-0000-0000-0000000000bb"

        with pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_api_key_no_project_scope_raises_403(
        self, db_session: AsyncMock,
    ) -> None:
        """API key with no project scope → raises 403."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.user_id = ORG_ID_STR
        request.state.auth_type = "api_key"
        request.state.api_key_project_id = None

        with pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_auth_type_raises_401(
        self, db_session: AsyncMock,
    ) -> None:
        """auth_type is neither jwt nor api_key → falls through to user_id check → 401."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.user_id = ORG_ID_STR
        request.state.auth_type = "unknown"
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 401
