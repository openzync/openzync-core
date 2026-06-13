"""Project service — business logic for project and membership management.

Provides create, read, update, delete, and membership operations for
projects. All DB access is delegated to ``ProjectRepository``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from core.exceptions import ConflictError, NotFoundError, ValidationError
from schemas.projects import (
    MemberResponse,
    ProjectResponse,
)
from repositories.project_repository import ProjectRepository

logger = logging.getLogger(__name__)


class ProjectService:
    """Business logic for project and membership management.

    Args:
        repo: The project repository.
    """

    def __init__(self, repo: ProjectRepository) -> None:
        self._repo = repo

    # ── Project CRUD ─────────────────────────────────────────────────────────

    async def create_project(
        self,
        organization_id: UUID,
        name: str,
        description: str | None = None,
    ) -> ProjectResponse:
        """Create a new project within an organization.

        Args:
            organization_id: The owning organization UUID.
            name: Human-readable project name (unique within org).
            description: Optional description.

        Returns:
            The newly created project response.

        Raises:
            ConflictError: A project with this name already exists.
        """
        # Check for duplicates — the unique constraint catches races too
        projects, _ = await self._repo.list_by_org(organization_id, limit=200)
        for p in projects:
            if p.name == name:
                raise ConflictError(
                    f"Project '{name}' already exists in this organization"
                )

        project = await self._repo.create(
            organization_id=organization_id,
            name=name,
            description=description,
        )

        logger.info(
            "project.created",
            extra={
                "project_id": str(project.id),
                "organization_id": str(organization_id),
                "project_name": name,
            },
        )

        return ProjectResponse.model_validate(project)

    async def get_project(
        self,
        organization_id: UUID,
        project_id: UUID,
    ) -> ProjectResponse:
        """Get a project by UUID.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project's UUID.

        Returns:
            The project response.

        Raises:
            NotFoundError: Project not found.
        """
        project = await self._repo.get_by_id(organization_id, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")
        return ProjectResponse.model_validate(project)

    async def list_projects(
        self,
        organization_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[ProjectResponse], str | None]:
        """List projects for an organization.

        Args:
            organization_id: The owning organization UUID.
            limit: Maximum items per page.
            cursor: Opaque cursor for pagination.

        Returns:
            A tuple of (project responses, next cursor).
        """
        projects, next_cursor = await self._repo.list_by_org(
            organization_id,
            limit=limit,
            cursor=cursor,
        )
        items = [ProjectResponse.model_validate(p) for p in projects]
        return items, next_cursor

    async def update_project(
        self,
        organization_id: UUID,
        project_id: UUID,
        name: str | None = None,
        description: str | None = None,
        is_active: bool | None = None,
    ) -> ProjectResponse:
        """Update a project's fields.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project's UUID.
            name: New name (if provided).
            description: New description (if provided).
            is_active: New active state (if provided).

        Returns:
            The updated project response.

        Raises:
            NotFoundError: Project not found.
        """
        if name is not None:
            if not name.strip():
                raise ValidationError("Project name cannot be empty")

        project = await self._repo.update(
            organization_id=organization_id,
            project_id=project_id,
            name=name,
            description=description,
            is_active=is_active,
        )
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")

        logger.info(
            "project.updated",
            extra={"project_id": str(project_id), "project_name": name},
        )
        return ProjectResponse.model_validate(project)

    async def delete_project(
        self,
        organization_id: UUID,
        project_id: UUID,
    ) -> None:
        """Soft-delete a project.

        Args:
            organization_id: The owning organization UUID.
            project_id: The project's UUID.

        Raises:
            NotFoundError: Project not found.
        """
        project = await self._repo.soft_delete(organization_id, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")

        logger.info("project.deleted", extra={"project_id": str(project_id)})

    # ── Member Management ────────────────────────────────────────────────────

    async def add_member(
        self,
        organization_id: UUID,
        project_id: UUID,
        user_id: UUID,
        role: str = "member",
    ) -> MemberResponse:
        """Add a user to a project.

        Args:
            organization_id: The owning organization UUID (for verification).
            project_id: The project's UUID.
            user_id: The user's UUID.
            role: Project role (admin/member/viewer).

        Returns:
            The created member response.

        Raises:
            NotFoundError: Project not found.
            ConflictError: User is already a member.
        """
        # Verify project exists
        project = await self._repo.get_by_id(organization_id, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")

        # Check not already a member
        if await self._repo.is_member(project_id, user_id):
            raise ConflictError(
                f"User {user_id} is already a member of project {project_id}"
            )

        member = await self._repo.add_member(project_id, user_id, role)
        return MemberResponse.model_validate(member)

    async def remove_member(
        self,
        organization_id: UUID,
        project_id: UUID,
        user_id: UUID,
    ) -> None:
        """Remove a user from a project.

        Args:
            organization_id: The owning organization UUID (for verification).
            project_id: The project's UUID.
            user_id: The user's UUID.

        Raises:
            NotFoundError: Project or member not found.
        """
        project = await self._repo.get_by_id(organization_id, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")

        removed = await self._repo.remove_member(project_id, user_id)
        if not removed:
            raise NotFoundError(
                f"User {user_id} is not a member of project {project_id}"
            )

    async def list_members(
        self,
        organization_id: UUID,
        project_id: UUID,
    ) -> list[MemberResponse]:
        """List all members of a project.

        Args:
            organization_id: The owning organization UUID (for verification).
            project_id: The project's UUID.

        Returns:
            A list of member responses.

        Raises:
            NotFoundError: Project not found.
        """
        project = await self._repo.get_by_id(organization_id, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")

        members = await self._repo.list_members(project_id)
        return [MemberResponse.model_validate(m) for m in members]

    async def update_member_role(
        self,
        organization_id: UUID,
        project_id: UUID,
        user_id: UUID,
        role: str,
    ) -> MemberResponse:
        """Update a member's role within a project.

        Args:
            organization_id: The owning organization UUID (for verification).
            project_id: The project's UUID.
            user_id: The user's UUID.
            role: New role (admin/member/viewer).

        Returns:
            The updated member response.

        Raises:
            NotFoundError: Project or member not found.
        """
        project = await self._repo.get_by_id(organization_id, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")

        member = await self._repo.update_member_role(project_id, user_id, role)
        if member is None:
            raise NotFoundError(
                f"User {user_id} is not a member of project {project_id}"
            )
        return MemberResponse.model_validate(member)

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
            ``True`` if the user is a member.
        """
        return await self._repo.is_member(project_id, user_id)
