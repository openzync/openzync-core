"""Integration tests for ProjectRepository — CRUD with real PostgreSQL.

Requires testcontainers PostgreSQL (provided by ``tests/integration/conftest.py``).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.project_repository import ProjectRepository
from repositories.user_repository import UserRepository


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
class TestProjectRepository:
    """ProjectRepository CRUD + member management tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    async def _seed_user(
        self, repo: UserRepository, ext_id: str = "project_test_user"
    ) -> UUID:
        user = await repo.create(
            organization_id=self.ORG_ID,
            external_id=ext_id,
            name="Project Tester",
        )
        return user.id

    # ── Create ───────────────────────────────────────────────────────────────

    async def test_create_project(self, engine) -> None:
        """Creating a project returns the project with generated fields."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)

            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Test Project",
                created_by=user_id,
                description="A test project",
            )
            assert project.id is not None
            assert project.name == "Test Project"
            assert project.description == "A test project"
            assert project.created_by == user_id
            assert project.is_archived is False
            assert project.created_at is not None
            assert project.updated_at is not None

    async def test_create_project_with_metadata(self, engine) -> None:
        """Creating a project with metadata stores it."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)

            meta = {"department": "engineering", "tier": "gold"}
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Metadata Project",
                created_by=user_id,
                metadata_=meta,
            )
            assert project.metadata_ == meta

    # ── Get By ID ────────────────────────────────────────────────────────────

    async def test_get_by_id_found(self, engine) -> None:
        """get_by_id returns the project."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Find Me",
                created_by=user_id,
            )

            found = await repo.get_by_id(self.ORG_ID, project.id)
            assert found is not None
            assert found.id == project.id
            assert found.name == "Find Me"

    async def test_get_by_id_not_found(self, engine) -> None:
        """get_by_id returns None for non-existent project."""
        async with AsyncSession(engine) as db:
            repo = ProjectRepository(db)

            found = await repo.get_by_id(
                self.ORG_ID,
                UUID("00000000-0000-0000-0000-000000000099"),
            )
            assert found is None

    async def test_get_by_id_archived_not_returned(self, engine) -> None:
        """get_by_id does not return archived projects."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Archive Me",
                created_by=user_id,
            )
            await repo.archive(self.ORG_ID, project.id)

            found = await repo.get_by_id(self.ORG_ID, project.id)
            assert found is None

    # ── Get By Name ──────────────────────────────────────────────────────────

    async def test_get_by_name_found(self, engine) -> None:
        """get_by_name returns the project by name."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            await repo.create(
                organization_id=self.ORG_ID,
                name="Unique Name",
                created_by=user_id,
            )

            found = await repo.get_by_name(self.ORG_ID, "Unique Name")
            assert found is not None
            assert found.name == "Unique Name"

    async def test_get_by_name_not_found(self, engine) -> None:
        """get_by_name returns None for non-existent name."""
        async with AsyncSession(engine) as db:
            repo = ProjectRepository(db)

            found = await repo.get_by_name(self.ORG_ID, "Non-existent")
            assert found is None

    # ── List ─────────────────────────────────────────────────────────────────

    async def test_list_returns_only_member_projects(self, engine) -> None:
        """list returns only projects where the user is a member."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)

            p1 = await repo.create(
                organization_id=self.ORG_ID,
                name="Project A",
                created_by=user_id,
            )
            p2 = await repo.create(
                organization_id=self.ORG_ID,
                name="Project B",
                created_by=user_id,
            )
            # Add user as member to both
            await repo.add_member(project_id=p1.id, user_id=user_id, role="owner")
            await repo.add_member(project_id=p2.id, user_id=user_id, role="member")

            projects = await repo.list(
                organization_id=self.ORG_ID,
                user_id=user_id,
            )
            assert len(projects) == 2
            names = {p.name for p in projects}
            assert names == {"Project A", "Project B"}

    async def test_list_excludes_archived(self, engine) -> None:
        """list excludes archived projects."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)

            p1 = await repo.create(
                organization_id=self.ORG_ID,
                name="Active Project",
                created_by=user_id,
            )
            p2 = await repo.create(
                organization_id=self.ORG_ID,
                name="Archived Project",
                created_by=user_id,
            )
            await repo.add_member(project_id=p1.id, user_id=user_id, role="owner")
            await repo.add_member(project_id=p2.id, user_id=user_id, role="owner")
            await repo.archive(self.ORG_ID, p2.id)

            projects = await repo.list(
                organization_id=self.ORG_ID,
                user_id=user_id,
            )
            assert len(projects) == 1
            assert projects[0].name == "Active Project"

    async def test_list_excludes_non_member_projects(self, engine) -> None:
        """list excludes projects the user is not a member of."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo, "member_user")
            other_user_id = await self._seed_user(user_repo, "other_user")

            p = await repo.create(
                organization_id=self.ORG_ID,
                name="Other's Project",
                created_by=other_user_id,
            )
            await repo.add_member(
                project_id=p.id, user_id=other_user_id, role="owner"
            )

            projects = await repo.list(
                organization_id=self.ORG_ID,
                user_id=user_id,
            )
            assert len(projects) == 0

    # ── Update ───────────────────────────────────────────────────────────────

    async def test_update_project_name(self, engine) -> None:
        """Update changes the project name."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Original Name",
                created_by=user_id,
            )

            updated = await repo.update(
                organization_id=self.ORG_ID,
                project_id=project.id,
                name="Updated Name",
            )
            assert updated is not None
            assert updated.name == "Updated Name"

    async def test_update_project_description(self, engine) -> None:
        """Update changes the project description."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Desc Test",
                created_by=user_id,
                description="Original description",
            )

            updated = await repo.update(
                organization_id=self.ORG_ID,
                project_id=project.id,
                description="Updated description",
            )
            assert updated is not None
            assert updated.description == "Updated description"

    async def test_update_not_found(self, engine) -> None:
        """Update on non-existent project returns None."""
        async with AsyncSession(engine) as db:
            repo = ProjectRepository(db)

            result = await repo.update(
                organization_id=self.ORG_ID,
                project_id=UUID("00000000-0000-0000-0000-000000000099"),
                name="Ghost",
            )
            assert result is None

    # ── Archive ──────────────────────────────────────────────────────────────

    async def test_archive_project(self, engine) -> None:
        """Archive marks is_archived as True."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Archivable",
                created_by=user_id,
            )

            archived = await repo.archive(self.ORG_ID, project.id)
            assert archived is not None
            assert archived.is_archived is True

    async def test_archive_not_found(self, engine) -> None:
        """Archive on non-existent project returns None."""
        async with AsyncSession(engine) as db:
            repo = ProjectRepository(db)

            result = await repo.archive(
                self.ORG_ID,
                UUID("00000000-0000-0000-0000-000000000099"),
            )
            assert result is None

    # ── Member Management ────────────────────────────────────────────────────

    async def test_add_member(self, engine) -> None:
        """Adding a member creates a membership record."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Member Test",
                created_by=user_id,
            )

            member = await repo.add_member(
                project_id=project.id,
                user_id=user_id,
                role="member",
            )
            assert member.id is not None
            assert member.project_id == project.id
            assert member.user_id == user_id
            assert member.role == "member"

    async def test_remove_member(self, engine) -> None:
        """Removing a member returns True."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Remove Test",
                created_by=user_id,
            )
            await repo.add_member(
                project_id=project.id, user_id=user_id, role="member"
            )

            result = await repo.remove_member(project.id, user_id)
            assert result is True

    async def test_remove_member_not_found(self, engine) -> None:
        """Removing a non-existent member returns False."""
        async with AsyncSession(engine) as db:
            repo = ProjectRepository(db)

            result = await repo.remove_member(
                UUID("00000000-0000-0000-0000-000000000099"),
                UUID("00000000-0000-0000-0000-000000000099"),
            )
            assert result is False

    async def test_get_member_found(self, engine) -> None:
        """get_member returns the membership."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Get Member",
                created_by=user_id,
            )
            await repo.add_member(
                project_id=project.id, user_id=user_id, role="owner"
            )

            member = await repo.get_member(project.id, user_id)
            assert member is not None
            assert member.role == "owner"

    async def test_get_member_not_found(self, engine) -> None:
        """get_member returns None for non-member."""
        async with AsyncSession(engine) as db:
            repo = ProjectRepository(db)

            member = await repo.get_member(
                UUID("00000000-0000-0000-0000-000000000099"),
                UUID("00000000-0000-0000-0000-000000000099"),
            )
            assert member is None

    async def test_list_members(self, engine) -> None:
        """list_members returns all members."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="List Members",
                created_by=user_id,
            )
            await repo.add_member(
                project_id=project.id, user_id=user_id, role="owner"
            )

            members = await repo.list_members(project.id)
            assert len(members) >= 1
            assert any(m.user_id == user_id for m in members)

    async def test_update_member_role(self, engine) -> None:
        """update_member_role changes the role."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Role Test",
                created_by=user_id,
            )
            await repo.add_member(
                project_id=project.id, user_id=user_id, role="member"
            )

            updated = await repo.update_member_role(
                project_id=project.id,
                user_id=user_id,
                role="owner",
            )
            assert updated is not None
            assert updated.role == "owner"

    async def test_update_member_role_not_found(self, engine) -> None:
        """update_member_role on non-existent returns None."""
        async with AsyncSession(engine) as db:
            repo = ProjectRepository(db)

            result = await repo.update_member_role(
                project_id=UUID("00000000-0000-0000-0000-000000000099"),
                user_id=UUID("00000000-0000-0000-0000-000000000099"),
                role="owner",
            )
            assert result is None

    async def test_count_members(self, engine) -> None:
        """count_members returns the correct count."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            repo = ProjectRepository(db)
            user_id = await self._seed_user(user_repo)
            project = await repo.create(
                organization_id=self.ORG_ID,
                name="Count Test",
                created_by=user_id,
            )
            await repo.add_member(
                project_id=project.id, user_id=user_id, role="owner"
            )

            count = await repo.count_members(project.id)
            assert count >= 1
