"""Unit tests for ProjectService — business logic with mocked repository.

Covers all CRUD and membership-management methods of ``ProjectService``.
Repository calls are mocked via ``AsyncMock`` so no database is required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import NotFoundError, ValidationError
from services.project_service import ProjectService

from schemas.projects import (
    AddMemberRequest,
    CreateProjectRequest,
    UpdateProjectRequest,
)


@pytest.mark.unit
class TestProjectService:
    """ProjectService unit tests — all repository calls are mocked."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[ProjectService, AsyncMock]:
        """Create a ProjectService with a mocked repository."""
        mock_repo = AsyncMock()
        service = ProjectService(repo=mock_repo)
        return service, mock_repo

    def _make_mock_project(self, **kwargs: object) -> MagicMock:
        """Mock a Project ORM instance."""
        project = MagicMock()
        project.id = kwargs.get("id", self.PROJECT_ID)
        project.organization_id = kwargs.get("org_id", self.ORG_ID)
        project.name = kwargs.get("name", "Test Project")
        project.description = kwargs.get("description", "A test project")
        project.created_by = kwargs.get("created_by", self.USER_ID)
        project.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
        project.updated_at = kwargs.get("updated_at", datetime.now(timezone.utc))
        return project

    def _make_mock_member(self, **kwargs: object) -> MagicMock:
        """Mock a ProjectMember ORM instance."""
        member = MagicMock()
        member.id = kwargs.get("id", uuid4())
        member.project_id = kwargs.get("project_id", self.PROJECT_ID)
        member.user_id = kwargs.get("user_id", self.USER_ID)
        member.role = kwargs.get("role", "owner")
        member.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
        return member

    # ── Create ───────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_create_project_success(self) -> None:
        """Creating a project returns the response."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = None
        mock_repo.create.return_value = self._make_mock_project(name="New Project")
        payload = CreateProjectRequest(name="New Project")

        result = await service.create_project(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            payload=payload,
        )
        assert result.name == "New Project"
        mock_repo.create.assert_awaited_once()
        mock_repo.add_member.assert_awaited_once_with(
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            role="owner",
        )

    @pytest.mark.asyncio
    async def test_create_project_duplicate_name_raises_validation(self) -> None:
        """Creating a project with a duplicate name raises ValidationError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = self._make_mock_project()
        payload = CreateProjectRequest(name="Existing Project")

        with pytest.raises(ValidationError):
            await service.create_project(
                organization_id=self.ORG_ID,
                user_id=self.USER_ID,
                payload=payload,
            )
        mock_repo.create.assert_not_awaited()

    # ── Get ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_project_found(self) -> None:
        """Getting a project returns the response."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_mock_project()
        mock_repo.count_members.return_value = 3

        result = await service.get_project(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )
        assert result.id == self.PROJECT_ID
        assert result.name == "Test Project"
        assert result.member_count == 3

    @pytest.mark.asyncio
    async def test_get_project_not_found_raises_404(self) -> None:
        """Getting a non-existent project raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_project(
                organization_id=self.ORG_ID,
                project_id=uuid4(),
            )

    # ── List ─────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_list_projects_returns_projects(self) -> None:
        """Listing projects returns all projects the user is a member of."""
        service, mock_repo = self._make_service()
        mock_projects = [
            self._make_mock_project(name="Project A", id=uuid4()),
            self._make_mock_project(name="Project B", id=uuid4()),
        ]
        mock_repo.list.return_value = mock_projects
        mock_repo.count_members_for_projects.return_value = {
            mock_projects[0].id: 5,
            mock_projects[1].id: 3,
        }

        results = await service.list_projects(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            limit=10,
            offset=0,
        )
        assert len(results) == 2
        assert results[0].name == "Project A"
        assert results[0].member_count == 5
        assert results[1].member_count == 3
        mock_repo.list.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            limit=10,
            offset=0,
        )

    @pytest.mark.asyncio
    async def test_list_projects_empty(self) -> None:
        """Listing projects returns empty list when the user has none."""
        service, mock_repo = self._make_service()
        mock_repo.list.return_value = []
        mock_repo.count_members_for_projects.return_value = {}

        results = await service.list_projects(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
        )
        assert results == []

    # ── Update ───────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_project_success(self) -> None:
        """Updating a project returns the updated response."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = None
        mock_repo.update.return_value = self._make_mock_project(
            name="Updated Name",
            description="Updated description",
        )
        mock_repo.count_members.return_value = 2
        payload = UpdateProjectRequest(
            name="Updated Name",
            description="Updated description",
        )

        result = await service.update_project(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            payload=payload,
        )
        assert result.name == "Updated Name"
        assert result.description == "Updated description"
        assert result.member_count == 2

    @pytest.mark.asyncio
    async def test_update_project_duplicate_name_raises_validation(self) -> None:
        """Updating with a conflicting name raises ValidationError."""
        service, mock_repo = self._make_service()
        other_project = self._make_mock_project(
            id=uuid4(),  # different project
            name="Conflicting Name",
        )
        mock_repo.get_by_name.return_value = other_project
        payload = UpdateProjectRequest(name="Conflicting Name")

        with pytest.raises(ValidationError):
            await service.update_project(
                organization_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                payload=payload,
            )
        mock_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_project_self_name_no_conflict(self) -> None:
        """Updating with the same name does not raise (name belongs to self)."""
        service, mock_repo = self._make_service()
        same_project = self._make_mock_project(name="Same Name")
        mock_repo.get_by_name.return_value = same_project
        mock_repo.update.return_value = same_project
        mock_repo.count_members.return_value = 2
        payload = UpdateProjectRequest(name="Same Name")

        result = await service.update_project(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            payload=payload,
        )
        assert result.name == "Same Name"
        assert result.member_count == 2
        mock_repo.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_project_not_found_raises_404(self) -> None:
        """Updating a non-existent project raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = None
        mock_repo.update.return_value = None
        payload = UpdateProjectRequest(name="Ghost Project")

        with pytest.raises(NotFoundError):
            await service.update_project(
                organization_id=self.ORG_ID,
                project_id=uuid4(),
                payload=payload,
            )

    # ── Archive ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_archive_project_success(self) -> None:
        """Archiving a project succeeds."""
        service, mock_repo = self._make_service()
        mock_repo.archive.return_value = self._make_mock_project()

        await service.archive_project(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )
        mock_repo.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_archive_project_not_found_raises_404(self) -> None:
        """Archiving a non-existent project raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.archive.return_value = None

        with pytest.raises(NotFoundError):
            await service.archive_project(
                organization_id=self.ORG_ID,
                project_id=uuid4(),
            )

    # ── Add Member ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_member_success(self) -> None:
        """Adding a member returns the membership."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = None
        new_user_id = uuid4()
        mock_repo.add_member.return_value = self._make_mock_member(
            user_id=new_user_id,
            role="member",
        )
        payload = AddMemberRequest(user_id=new_user_id, role="member")

        result = await service.add_member(
            project_id=self.PROJECT_ID,
            payload=payload,
        )
        assert result.user_id == new_user_id
        assert result.role == "member"

    @pytest.mark.asyncio
    async def test_add_member_duplicate_raises_validation(self) -> None:
        """Adding an existing member raises ValidationError."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = self._make_mock_member()
        payload = AddMemberRequest(user_id=self.USER_ID, role="member")

        with pytest.raises(ValidationError):
            await service.add_member(
                project_id=self.PROJECT_ID,
                payload=payload,
            )
        mock_repo.add_member.assert_not_awaited()

    # ── Remove Member ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_remove_member_success(self) -> None:
        """Removing a member succeeds."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = self._make_mock_member(role="member")
        mock_repo.list_members.return_value = [
            self._make_mock_member(role="owner", user_id=self.USER_ID),
            self._make_mock_member(role="member", user_id=uuid4()),
        ]
        mock_repo.remove_member.return_value = True

        await service.remove_member(
            project_id=self.PROJECT_ID,
            user_id=uuid4(),
        )
        mock_repo.remove_member.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_last_owner_raises_validation(self) -> None:
        """Removing the last owner raises ValidationError."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = self._make_mock_member(role="owner")
        mock_repo.list_members.return_value = [
            self._make_mock_member(role="owner", user_id=self.USER_ID),
        ]

        with pytest.raises(ValidationError):
            await service.remove_member(
                project_id=self.PROJECT_ID,
                user_id=self.USER_ID,
            )
        mock_repo.remove_member.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remove_member_not_found_raises_404(self) -> None:
        """Removing a non-existent member raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = None

        with pytest.raises(NotFoundError):
            await service.remove_member(
                project_id=self.PROJECT_ID,
                user_id=uuid4(),
            )

    @pytest.mark.asyncio
    async def test_remove_member_repo_fails_raises_404(self) -> None:
        """When the repo returns False, raise NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = self._make_mock_member(role="member")
        mock_repo.list_members.return_value = [
            self._make_mock_member(role="owner", user_id=self.USER_ID),
            self._make_mock_member(role="member", user_id=uuid4()),
        ]
        mock_repo.remove_member.return_value = False

        with pytest.raises(NotFoundError):
            await service.remove_member(
                project_id=self.PROJECT_ID,
                user_id=uuid4(),
            )

    # ── List Members ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_list_members_returns_members(self) -> None:
        """Listing members returns all members."""
        service, mock_repo = self._make_service()
        mock_repo.list_members.return_value = [
            self._make_mock_member(role="owner"),
            self._make_mock_member(role="member", user_id=uuid4()),
        ]

        results = await service.list_members(project_id=self.PROJECT_ID)
        assert len(results) == 2
        assert results[0].role == "owner"
        assert results[1].role == "member"

    # ── Update Member Role ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_member_role_success(self) -> None:
        """Changing a member's role succeeds."""
        service, mock_repo = self._make_service()
        target_user = uuid4()
        mock_repo.get_member.return_value = self._make_mock_member(
            user_id=target_user, role="member"
        )
        mock_repo.update_member_role.return_value = self._make_mock_member(
            user_id=target_user, role="owner"
        )

        result = await service.update_member_role(
            project_id=self.PROJECT_ID,
            user_id=target_user,
            role="owner",
        )
        assert result.role == "owner"

    @pytest.mark.asyncio
    async def test_update_member_role_last_owner_downgrade_raises(self) -> None:
        """Downgrading the last owner raises ValidationError."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = self._make_mock_member(role="owner")
        mock_repo.list_members.return_value = [
            self._make_mock_member(role="owner", user_id=self.USER_ID),
        ]

        with pytest.raises(ValidationError):
            await service.update_member_role(
                project_id=self.PROJECT_ID,
                user_id=self.USER_ID,
                role="member",
            )
        mock_repo.update_member_role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_member_role_not_found_raises_404(self) -> None:
        """Updating a non-existent member raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = None

        with pytest.raises(NotFoundError):
            await service.update_member_role(
                project_id=self.PROJECT_ID,
                user_id=uuid4(),
                role="owner",
            )

    @pytest.mark.asyncio
    async def test_update_member_role_repo_fails_raises_404(self) -> None:
        """When the repo returns None after get_member succeeds, raise NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_member.return_value = self._make_mock_member(role="member")
        mock_repo.list_members.return_value = [
            self._make_mock_member(role="owner", user_id=self.USER_ID),
            self._make_mock_member(role="member", user_id=uuid4()),
        ]
        mock_repo.update_member_role.return_value = None

        with pytest.raises(NotFoundError):
            await service.update_member_role(
                project_id=self.PROJECT_ID,
                user_id=uuid4(),
                role="owner",
            )
