"""Unit tests for ProjectRepository — project and member CRUD."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.project_repository import ProjectRepository


pytestmark = pytest.mark.unit


class TestProjectRepository:
    """ProjectRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000010")
    USER_ID = UUID("00000000-0000-0000-0000-000000000020")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> ProjectRepository:
        return ProjectRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_project(self, **overrides: object) -> MagicMock:
        p = MagicMock()
        p.id = overrides.get("id", self.PROJECT_ID)
        p.organization_id = overrides.get("organization_id", self.ORG_ID)
        p.name = overrides.get("name", "Test Project")
        p.description = overrides.get("description", "A test project")
        p.created_by = overrides.get("created_by", self.USER_ID)
        p.metadata_ = overrides.get("metadata_", {})
        p.is_archived = overrides.get("is_archived", False)
        p.created_at = overrides.get("created_at", None)
        return p

    def _mock_member(self, **overrides: object) -> MagicMock:
        m = MagicMock()
        m.id = overrides.get("id", uuid4())
        m.project_id = overrides.get("project_id", self.PROJECT_ID)
        m.user_id = overrides.get("user_id", self.USER_ID)
        m.role = overrides.get("role", "member")
        m.created_at = overrides.get("created_at", None)
        return m

    # ── create ─────────────────────────────────────────────────────────────────

    async def test_create(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """create inserts and returns a new project."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID,
            name="New Project",
            created_by=self.USER_ID,
            description="desc",
            metadata_={"key": "val"},
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_minimal(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """create works with minimal fields."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID, name="Minimal"
        )

        assert result is not None

    # ── get_by_id ──────────────────────────────────────────────────────────────

    async def test_get_by_id_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns project when found."""
        project = self._mock_project()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(
            organization_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert result == project

    async def test_get_by_id_not_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(
            organization_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert result is None

    # ── get_by_name ────────────────────────────────────────────────────────────

    async def test_get_by_name_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_name returns project when name matches."""
        project = self._mock_project(name="My Project")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_name(
            organization_id=self.ORG_ID, name="My Project"
        )

        assert result == project

    async def test_get_by_name_not_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_name returns None when name does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_name(
            organization_id=self.ORG_ID, name="Nonexistent"
        )

        assert result is None

    # ── list ───────────────────────────────────────────────────────────────────

    async def test_list(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """list returns projects for an org."""
        projects = [self._mock_project(), self._mock_project(id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = projects
        mock_db.execute.return_value = mock_result

        result = await repo.list(
            organization_id=self.ORG_ID, user_id=None
        )

        assert result == projects

    async def test_list_with_user_filter(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """list filters by user_id when provided."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.list(
            organization_id=self.ORG_ID, user_id=self.USER_ID
        )

        assert result == []

    async def test_list_with_pagination(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """list respects limit and offset."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.list(
            organization_id=self.ORG_ID, user_id=None, limit=10, offset=5
        )

        assert result == []

    # ── update ─────────────────────────────────────────────────────────────────

    async def test_update(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """update modifies project fields."""
        project = self._mock_project()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_result
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.update(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            name="Updated",
            description="New desc",
        )

        assert result is not None
        assert result.name == "Updated"
        assert result.description == "New desc"
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_update_partial(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """update only changes provided fields."""
        project = self._mock_project(name="Original", description="Desc")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_result

        result = await repo.update(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            name="Only Name",
        )

        assert result.name == "Only Name"
        assert result.description == "Desc"  # unchanged

    async def test_update_not_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """update returns None when project not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.update(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            name="Nope",
        )

        assert result is None

    # ── archive ────────────────────────────────────────────────────────────────

    async def test_archive(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """archive sets is_archived flag."""
        project = self._mock_project(is_archived=False)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_result

        result = await repo.archive(
            organization_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert result is not None
        assert result.is_archived is True
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_archive_not_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """archive returns None when project not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.archive(
            organization_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert result is None

    # ── count_active ───────────────────────────────────────────────────────────

    async def test_count_active(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """count_active returns the count."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_db.execute.return_value = mock_result

        count = await repo.count_active(organization_id=self.ORG_ID)

        assert count == 5

    async def test_count_active_zero(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """count_active returns 0 when none."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_db.execute.return_value = mock_result

        count = await repo.count_active(organization_id=self.ORG_ID)

        assert count == 0

    # ── add_member ─────────────────────────────────────────────────────────────

    async def test_add_member(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """add_member adds a user to a project."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.add_member(
            project_id=self.PROJECT_ID, user_id=self.USER_ID, role="owner"
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    # ── remove_member ──────────────────────────────────────────────────────────

    async def test_remove_member(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """remove_member removes a user from a project."""
        member = self._mock_member()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = member
        mock_db.execute.return_value = mock_result

        result = await repo.remove_member(
            project_id=self.PROJECT_ID, user_id=self.USER_ID
        )

        assert result is True
        mock_db.delete.assert_awaited_once_with(member)
        mock_db.flush.assert_awaited_once()

    async def test_remove_member_not_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """remove_member returns False when membership not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.remove_member(
            project_id=self.PROJECT_ID, user_id=self.USER_ID
        )

        assert result is False
        mock_db.delete.assert_not_called()

    # ── get_member ─────────────────────────────────────────────────────────────

    async def test_get_member_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """get_member returns membership when found."""
        member = self._mock_member()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = member
        mock_db.execute.return_value = mock_result

        result = await repo.get_member(
            project_id=self.PROJECT_ID, user_id=self.USER_ID
        )

        assert result == member

    async def test_get_member_not_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """get_member returns None when not a member."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_member(
            project_id=self.PROJECT_ID, user_id=self.USER_ID
        )

        assert result is None

    # ── list_members ───────────────────────────────────────────────────────────

    async def test_list_members(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """list_members returns all members of a project."""
        members = [self._mock_member(), self._mock_member(user_id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = members
        mock_db.execute.return_value = mock_result

        result = await repo.list_members(project_id=self.PROJECT_ID)

        assert result == members

    async def test_list_members_empty(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """list_members returns empty list."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.list_members(project_id=self.PROJECT_ID)

        assert result == []

    # ── update_member_role ─────────────────────────────────────────────────────

    async def test_update_member_role(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """update_member_role changes a member's role."""
        member = self._mock_member(role="member")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = member
        mock_db.execute.return_value = mock_result

        result = await repo.update_member_role(
            project_id=self.PROJECT_ID, user_id=self.USER_ID, role="owner"
        )

        assert result is not None
        assert result.role == "owner"
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_update_member_role_not_found(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """update_member_role returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.update_member_role(
            project_id=self.PROJECT_ID, user_id=self.USER_ID, role="owner"
        )

        assert result is None

    # ── count_members ──────────────────────────────────────────────────────────

    async def test_count_members(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """count_members returns the member count."""
        members = [self._mock_member(), self._mock_member(user_id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = members
        mock_db.execute.return_value = mock_result

        count = await repo.count_members(project_id=self.PROJECT_ID)

        assert count == 2

    async def test_count_members_zero(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """count_members returns 0 when no members."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        count = await repo.count_members(project_id=self.PROJECT_ID)

        assert count == 0

    # ── count_members_for_projects ─────────────────────────────────────────────

    async def test_count_members_for_projects(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """count_members_for_projects returns batch counts."""
        row_1 = (self.PROJECT_ID, 3)
        row_2 = (UUID("00000000-0000-0000-0000-000000000011"), 1)
        mock_db.execute.return_value = MagicMock()
        mock_db.execute.return_value.all.return_value = [row_1, row_2]

        result = await repo.count_members_for_projects(
            project_ids=[self.PROJECT_ID, UUID("00000000-0000-0000-0000-000000000011")]
        )

        assert result[self.PROJECT_ID] == 3

    async def test_count_members_for_projects_empty(
        self, repo: ProjectRepository, mock_db: AsyncMock
    ) -> None:
        """count_members_for_projects returns empty dict for empty input."""
        result = await repo.count_members_for_projects(project_ids=[])

        assert result == {}
