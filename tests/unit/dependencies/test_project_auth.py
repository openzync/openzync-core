"""Unit tests for dependencies/project_auth.py — project-scoped auth checks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit

PROJECT_ID = UUID("00000000-0000-0000-0000-0000000000aa")
ORG_ID_STR = "00000000-0000-0000-0000-000000000001"
USER_ID_STR = "00000000-0000-0000-0000-000000000002"


class TestRequireProjectMembership:
    """require_project_membership: unified JWT/API-key project auth."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def mock_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.get_by_id = AsyncMock()
        repo.get_member = AsyncMock()
        return repo

    @pytest.mark.asyncio
    async def test_jwt_valid_member_passes(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """JWT user who is a project member → passes (returns None)."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_by_id.return_value = MagicMock()
        mock_repo.get_member.return_value = MagicMock()

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ):
            result = await require_project_membership(request, PROJECT_ID, db_session)

        assert result is None
        mock_repo.get_by_id.assert_awaited_once_with(
            organization_id=ORG_ID_STR, project_id=PROJECT_ID
        )
        mock_repo.get_member.assert_awaited_once_with(
            project_id=PROJECT_ID, user_id=UUID(USER_ID_STR)
        )

    @pytest.mark.asyncio
    async def test_jwt_non_member_raises_403(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """JWT user who is NOT a member → raises 403."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_by_id.return_value = MagicMock()
        mock_repo.get_member.return_value = None  # not a member

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ):
            with pytest.raises(HTTPException) as exc:
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
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_by_id.return_value = None  # project not found
        mock_repo.get_member = AsyncMock()

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ):
            with pytest.raises(HTTPException) as exc:
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
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_org_id_raises_401(
        self, db_session: AsyncMock,
    ) -> None:
        """Missing org_id → raises 401."""
        from dependencies.project_auth import require_project_membership

        request = MagicMock(spec=Request)
        request.state.org_id = None

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
        request.state.org_id = ORG_ID_STR
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
        request.state.org_id = ORG_ID_STR
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
        request.state.org_id = ORG_ID_STR
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
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "unknown"
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await require_project_membership(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 401


class TestRequireProjectOwner:
    """require_project_owner: owner-level project auth."""

    @pytest.fixture
    def db_session(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def mock_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.get_by_id = AsyncMock()
        repo.get_member = AsyncMock()
        return repo

    @pytest.mark.asyncio
    async def test_owner_passes(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """JWT user with owner role → passes."""
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_by_id.return_value = MagicMock()
        mock_member = MagicMock()
        mock_member.role = "owner"
        mock_repo.get_member.return_value = mock_member

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ):
            result = await require_project_owner(request, PROJECT_ID, db_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_non_owner_raises_403(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """JWT user with non-owner role → raises 403."""
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_by_id.return_value = MagicMock()
        mock_member = MagicMock()
        mock_member.role = "member"
        mock_repo.get_member.return_value = mock_member

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ):
            with pytest.raises(HTTPException) as exc:
                await require_project_owner(request, PROJECT_ID, db_session)
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_api_key_raises_403(
        self, db_session: AsyncMock,
    ) -> None:
        """API key auth → raises 403 (owner ops require JWT)."""
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "api_key"

        with pytest.raises(HTTPException) as exc:
            await require_project_owner(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_user_id_raises_401(
        self, db_session: AsyncMock,
    ) -> None:
        """Missing user_id → raises 401."""
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = None

        with pytest.raises(HTTPException) as exc:
            await require_project_owner(request, PROJECT_ID, db_session)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_project_not_found_raises_404(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """Project does not exist → raises 404."""
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_by_id.return_value = None

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ):
            with pytest.raises(HTTPException) as exc:
                await require_project_owner(request, PROJECT_ID, db_session)
            assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_member_none_raises_403(
        self, db_session: AsyncMock, mock_repo: MagicMock
    ) -> None:
        """get_member returns None → raises 403."""
        from dependencies.project_auth import require_project_owner

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.state.auth_type = "jwt"
        request.state.user_id = USER_ID_STR

        mock_repo.get_by_id.return_value = MagicMock()
        mock_repo.get_member.return_value = None

        with patch(
            "dependencies.project_auth.ProjectRepository", return_value=mock_repo
        ):
            with pytest.raises(HTTPException) as exc:
                await require_project_owner(request, PROJECT_ID, db_session)
            assert exc.value.status_code == 403
