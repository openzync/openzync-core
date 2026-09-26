"""Project repository — all database access for Projects and ProjectMembers.

Every query is scoped to an ``organization_id``.  The repository returns
ORM models only — no business logic, no schema construction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import SortSpec, resolve_order_by
from models.project import Project
from models.project_member import ProjectMember

PROJECT_SORTABLE_COLUMNS = {
    "name": Project.name,
    "created_at": Project.created_at,
    "updated_at": Project.updated_at,
}
"""Sortable columns for GET /v1/projects (default created_at/desc)."""

PROJECT_MEMBER_SORTABLE_COLUMNS = {
    "created_at": ProjectMember.created_at,
    "role": ProjectMember.role,
}
"""Sortable columns for project members (default created_at/asc)."""


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
        created_by: UUID | None = None,
        description: str | None = None,
        metadata_: dict | None = None,
    ) -> Project:
        """Create a new project.

        Args:
            organization_id: Tenant scope.
            name: Human-readable project name (unique within org).
            created_by: Optional UUID of the user creating the project.
                ``None`` for API-key-authenticated requests.
            description: Optional project description.
            metadata_: Optional project metadata dict.

        Returns:
            The newly created Project.
        """
        project = Project(
            organization_id=organization_id,
            name=name,
            description=description if description is not None else "",
            created_by=created_by,
            metadata_=metadata_ if metadata_ is not None else {},
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

    async def is_archived(
        self, organization_id: UUID, project_id: UUID
    ) -> bool:
        """Check whether a project is archived (or missing).

        Single-column SELECT scoped to the org. Fail-closed: returns
        ``True`` when the row is missing so episode workers skip work
        instead of burning billable I/O on a project that is gone.

        Worker-session note: episode workers set the ``app.org_id`` RLS
        GUC (not ``app.bypass_rls``), so the org-scoped predicate
        matches their session semantics — no unscoped variant needed.

        Args:
            organization_id: Tenant scope.
            project_id: The project's UUID.

        Returns:
            ``True`` if the project is archived or does not exist,
            ``False`` otherwise.
        """
        result = await self._db.execute(
            select(Project.is_archived).where(
                Project.id == project_id,
                Project.organization_id == organization_id,
            )
        )
        flag = result.scalar_one_or_none()
        if flag is None:
            return True
        return flag

    async def list(
        self,
        organization_id: UUID,
        user_id: UUID | None,
        limit: int = 50,
        offset: int = 0,
        sort: SortSpec | None = None,
    ) -> list[Project]:
        """List non-archived projects in an organisation.

        When ``user_id`` is provided, only projects where that user is a
        member are returned.  When ``user_id`` is ``None`` (API key auth),
        all non-archived projects in the org are returned.

        Default ``created_at/desc``; whitelist ``name``, ``created_at``,
        ``updated_at``.

        Args:
            organization_id: Tenant scope.
            user_id: The authenticated user's UUID, or ``None`` for
                API-key-authenticated requests.
            limit: Maximum results per page (capped at 200).
            offset: Number of results to skip.
            sort: Validated sort spec.

        Returns:
            A list of Project ORM instances.
        """
        effective_limit = min(limit, 200)
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("created_at", "desc")

        query = select(Project).where(
            Project.organization_id == organization_id,
            Project.is_archived.is_(False),
        )

        if user_id is not None:
            query = (
                query
                .join(ProjectMember, Project.id == ProjectMember.project_id)
                .where(ProjectMember.user_id == user_id)
            )

        result = await self._db.execute(
            query
            .order_by(
                *resolve_order_by(
                    PROJECT_SORTABLE_COLUMNS,
                    Project.id,
                    req_sort,
                    req_dir,
                    default_sort_by="created_at",
                    default_dir="desc",
                )
            )
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

    async def count_active(self, organization_id: UUID) -> int:
        """Count non-archived projects for the given organization.

        Args:
            organization_id: Tenant scope.

        Returns:
            Number of active projects (0 if none).
        """
        result = await self._db.execute(
            select(func.count(Project.id)).where(
                Project.organization_id == organization_id,
                Project.is_archived.is_(False),
            )
        )
        return result.scalar() or 0

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
        self, project_id: UUID,
        sort: SortSpec | None = None,
    ) -> list[ProjectMember]:
        """List all members of a project.

        Default ``created_at/asc``; whitelist ``created_at``, ``role``.

        Args:
            project_id: The project's UUID.
            sort: Validated sort spec.

        Returns:
            A list of ProjectMember ORM instances.
        """
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("created_at", "asc")
        result = await self._db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(
                *resolve_order_by(
                    PROJECT_MEMBER_SORTABLE_COLUMNS,
                    ProjectMember.id,
                    req_sort,
                    req_dir,
                    default_sort_by="created_at",
                    default_dir="asc",
                )
            )
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

    async def count_members_for_projects(
        self, project_ids: list[UUID]
    ) -> dict[UUID, int]:
        """Batch-count members for multiple projects (single query).

        Args:
            project_ids: List of project UUIDs to count members for.

        Returns:
            A dict mapping ``{project_id: member_count}``.  Projects with
            no members are omitted from the dict (caller should default
            to ``0``).
        """
        if not project_ids:
            return {}

        result = await self._db.execute(
            select(
                ProjectMember.project_id,
                func.count(ProjectMember.id).label("count"),
            )
            .where(ProjectMember.project_id.in_(project_ids))
            .group_by(ProjectMember.project_id)
        )
        return {row[0]: row[1] for row in result.all()}
