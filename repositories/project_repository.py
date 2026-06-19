"""Project repository — all database access for Projects and ProjectMembers.

Every query is scoped to an ``organization_id``.  The repository returns
ORM models only — no business logic, no schema construction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.project import Project
from models.project_member import ProjectMember


class ProjectRepository:
    """All database access for projects and project members.

    Args:
        db: An async SQLAlchemy session (request-scoped).
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Project CRUD ─────────────────────────────────────────────────────────

    async def create(
        self,
        organization_id: UUID,
        name: str,
        created_by: UUID,
        description: str | None = None,
        metadata_: dict | None = None,
    ) -> Project:
        """Create a new project.

        Args:
            organization_id: Tenant scope.
            name: Human-readable project name (unique within org).
            created_by: UUID of the user creating the project (becomes owner).
            description: Optional project description.
            metadata_: Optional project metadata dict.

        Returns:
            The newly created Project.
        """
        project = Project(
            organization_id=organization_id,
            name=name,
            description=description or "",
            created_by=created_by,
            metadata_=metadata_ or {},
        )
        self._db.add(project)
        await self._db.flush()
        await self._db.refresh(project)
        return project

    async def get_by_id(
        self, organization_id: UUID, project_id: UUID
    ) -> Project | None:
        """Look up a project by its UUID, scoped to the organisation.

        Args:
            organization_id: Tenant scope.
            project_id: The project's UUID primary key.

        Returns:
            The Project if found, ``None`` otherwise.
        """
        result = await self._db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.organization_id == organization_id,
                Project.is_archived.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_name(
        self, organization_id: UUID, name: str
    ) -> Project | None:
        """Look up a project by name within an organisation.

        Args:
            organization_id: Tenant scope.
            name: The project name (case-sensitive).

        Returns:
            The Project if found, ``None`` otherwise.
        """
        result = await self._db.execute(
            select(Project).where(
                Project.organization_id == organization_id,
                Project.name == name,
                Project.is_archived.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        organization_id: UUID,
        user_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Project]:
        """List non-archived projects in an organisation that the user is a member of.

        Args:
            organization_id: Tenant scope.
            user_id: The authenticated user's UUID — only projects where this
                user is a member are returned.
            limit: Maximum results per page (capped at 200).
            offset: Number of results to skip.

        Returns:
            A list of Project ORM instances.
        """
        effective_limit = min(limit, 200)
        result = await self._db.execute(
            select(Project)
            .join(ProjectMember, Project.id == ProjectMember.project_id)
            .where(
                Project.organization_id == organization_id,
                Project.is_archived.is_(False),
                ProjectMember.user_id == user_id,
            )
            .order_by(Project.created_at.desc())
            .limit(effective_limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def update(
        self,
        organization_id: UUID,
        project_id: UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> Project | None:
        """Update project fields. Only provided fields are changed.

        Args:
            organization_id: Tenant scope.
            project_id: The project's UUID.
            name: New project name.
            description: New project description.

        Returns:
            The updated Project, or ``None`` if not found.
        """
        project = await self.get_by_id(organization_id, project_id)
        if project is None:
            return None

        if name is not None:
            project.name = name
        if description is not None:
            project.description = description

        await self._db.flush()
        await self._db.refresh(project)
        return project

    async def archive(
        self, organization_id: UUID, project_id: UUID
    ) -> Project | None:
        """Soft-delete (archive) a project.

        All sessions and entities remain in the database but the project
        is hidden from list queries.  Project memberships are unaffected.

        Args:
            organization_id: Tenant scope.
            project_id: The project's UUID.

        Returns:
            The archived Project, or ``None`` if not found.
        """
        project = await self.get_by_id(organization_id, project_id)
        if project is None:
            return None

        project.is_archived = True
        await self._db.flush()
        await self._db.refresh(project)
        return project

    # ── Member Management ────────────────────────────────────────────────────

    async def add_member(
        self,
        project_id: UUID,
        user_id: UUID,
        role: str = "member",
    ) -> ProjectMember:
        """Add a user to a project with the given role.

        Args:
            project_id: The project's UUID.
            user_id: The user's UUID.
            role: One of ``"owner"`` or ``"member"``.

        Returns:
            The newly created ProjectMember.

        Raises:
            sqlalchemy.exc.IntegrityError: If the user is already a member
                or the project/user does not exist.
        """
        member = ProjectMember(
            project_id=project_id,
            user_id=user_id,
            role=role,
        )
        self._db.add(member)
        await self._db.flush()
        await self._db.refresh(member)
        return member

    async def remove_member(
        self, project_id: UUID, user_id: UUID
    ) -> bool:
        """Remove a user from a project.

        Args:
            project_id: The project's UUID.
            user_id: The user's UUID.

        Returns:
            ``True`` if the membership was removed, ``False`` if it did not exist.
        """
        result = await self._db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            return False
        await self._db.delete(member)
        await self._db.flush()
        return True

    async def get_member(
        self, project_id: UUID, user_id: UUID
    ) -> ProjectMember | None:
        """Check if a user is a member of a project and return their membership.

        Args:
            project_id: The project's UUID.
            user_id: The user's UUID.

        Returns:
            The ProjectMember if found, ``None`` otherwise.
        """
        result = await self._db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_members(
        self, project_id: UUID
    ) -> list[ProjectMember]:
        """List all members of a project.

        Args:
            project_id: The project's UUID.

        Returns:
            A list of ProjectMember ORM instances.
        """
        result = await self._db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.created_at.asc())
        )
        return list(result.scalars().all())

    async def update_member_role(
        self,
        project_id: UUID,
        user_id: UUID,
        role: str,
    ) -> ProjectMember | None:
        """Update a member's role within a project.

        Args:
            project_id: The project's UUID.
            user_id: The user's UUID.
            role: New role (``"owner"`` or ``"member"``).

        Returns:
            The updated ProjectMember, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            return None

        member.role = role
        await self._db.flush()
        await self._db.refresh(member)
        return member

    async def count_members(self, project_id: UUID) -> int:
        """Count the number of members in a project.

        Args:
            project_id: The project's UUID.

        Returns:
            Member count.
        """
        result = await self._db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
            )
        )
        return len(result.scalars().all())
