"""Project repository — all database access for Project and ProjectMember models.

Provides CRUD for projects and membership management within an organization.
All queries are scoped to an ``organization_id`` parameter.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.project import Project, ProjectMember


class ProjectRepository:
    """All database access for projects and members.

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
        description: str | None = None,
    ) -> Project:
        """Create a new project within an organization.

        Args:
            organization_id: The owning organization UUID.
            name: Human-readable project name (unique within org).
            description: Optional description.

        Returns:
            The newly created Project with generated id and timestamps.
        """
        project = Project(
            organization_id=organization_id,
            name=name,
            description=description,
        )
        self._db.add(project)
        await self._db.flush()
        await self._db.refresh(project)
        return project

    async def get_by_id(
        self,
        organization_id: UUID,
        project_id: UUID,
    ) -> Project | None:
        """Get a project by UUID, scoped to organization.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project's UUID.

        Returns:
            The Project if found and not deleted, ``None`` otherwise.
        """
        result = await self._db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.organization_id == organization_id,
                Project.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_org(
        self,
        organization_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Project], str | None]:
        """List projects for an organization.

        Args:
            organization_id: The owning organization UUID.
            limit: Maximum results (capped at 200).
            cursor: Opaque cursor for cursor-based pagination.

        Returns:
            A tuple of ``(projects, next_cursor)``.
        """
        effective_limit = min(limit, 200) + 1

        query = (
            select(Project)
            .where(
                Project.organization_id == organization_id,
                Project.is_deleted.is_(False),
            )
            .order_by(Project.created_at.desc())
            .limit(effective_limit)
        )

        result = await self._db.execute(query)
        rows = list(result.scalars().all())

        has_more = len(rows) == effective_limit
        projects = rows[:limit] if has_more else rows

        next_cursor: str | None = None
        if has_more and projects:
            last = projects[-1]
            raw = f"{last.created_at.isoformat()}|{last.id.hex}"
            import base64
            next_cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

        return projects, next_cursor

    async def update(
        self,
        organization_id: UUID,
        project_id: UUID,
        name: str | None = None,
        description: str | None = None,
        is_active: bool | None = None,
    ) -> Project | None:
        """Update a project's fields.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project's UUID.
            name: New name (if provided).
            description: New description (if provided).
            is_active: New active state (if provided).

        Returns:
            The updated Project, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.organization_id == organization_id,
                Project.is_deleted.is_(False),
            )
        )
        project = result.scalar_one_or_none()
        if project is None:
            return None

        if name is not None:
            project.name = name
        if description is not None:
            project.description = description
        if is_active is not None:
            project.is_active = is_active

        await self._db.flush()
        await self._db.refresh(project)
        return project

    async def soft_delete(
        self,
        organization_id: UUID,
        project_id: UUID,
    ) -> Project | None:
        """Soft-delete a project.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project's UUID.

        Returns:
            The updated Project, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.organization_id == organization_id,
                Project.is_deleted.is_(False),
            )
        )
        project = result.scalar_one_or_none()
        if project is None:
            return None

        project.is_deleted = True
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
        """Add a user to a project.

        Args:
            project_id: The project's UUID.
            user_id: The user's UUID.
            role: Project role (admin/member/viewer).

        Returns:
            The created ProjectMember.
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
        self,
        project_id: UUID,
        user_id: UUID,
    ) -> bool:
        """Remove a user from a project.

        Args:
            project_id: The project's UUID.
            user_id: The user's UUID.

        Returns:
            ``True`` if the member was removed, ``False`` if not found.
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

    async def list_members(
        self,
        project_id: UUID,
    ) -> list[ProjectMember]:
        """List all members of a project.

        Args:
            project_id: The project's UUID.

        Returns:
            A list of ProjectMember records.
        """
        result = await self._db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
            ).order_by(ProjectMember.created_at.asc())
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
            role: New role (admin/member/viewer).

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

    async def is_member(
        self,
        project_id: UUID,
        user_id: UUID,
    ) -> bool:
        """Check if a user is a member of a project.

        Args:
            project_id: The project's UUID.
            user_id: The user's UUID.

        Returns:
            ``True`` if the user is a member of the project.
        """
        result = await self._db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None
