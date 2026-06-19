"""Project service — orchestrates project and project member lifecycle.

Handles creation, discovery, membership management, and archiving of
project workspaces.  All methods are scoped to an ``organization_id``
for tenant isolation.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError, ValidationError
from repositories.project_repository import ProjectRepository
from schemas.projects import (
    AddMemberRequest,
    CreateProjectRequest,
    ProjectMemberResponse,
    ProjectResponse,
    UpdateProjectRequest,
)

logger = logging.getLogger(__name__)


class ProjectService:
    """Orchestrates project and member lifecycle operations.

    Args:
        repo: The project repository instance (request-scoped).
    """

    def __init__(self, repo: ProjectRepository) -> None:
        self._repo = repo

    # ── Create ──────────────────────────────────────────────────────────────

    async def create_project(
        self,
        organization_id: UUID,
        user_id: UUID,
        payload: CreateProjectRequest,
    ) -> ProjectResponse:
        """Create a new project and add the creator as an owner.

        Args:
            organization_id: Tenant scope.
            user_id: The creating user (becomes the initial owner).
            payload: Name, optional description, and optional metadata.

        Returns:
            The created ProjectResponse.

        Raises:
            ValidationError: If a project with the same name already exists
                in this organisation.
        """
        # Check for duplicate names within the org
        existing = await self._repo.get_by_name(
            organization_id=organization_id,
            name=payload.name,
        )
        if existing is not None:
            raise ValidationError(
                message=f"A project named '{payload.name}' already exists in this organisation",
                detail={"name": payload.name},
            )

        project = await self._repo.create(
            organization_id=organization_id,
            name=payload.name,
            created_by=user_id,
            description=payload.description,
            metadata_=payload.metadata,
        )

        # Add creator as owner
        await self._repo.add_member(
            project_id=project.id,
            user_id=user_id,
            role="owner",
        )

        logger.info(
            "project_service.project_created",
            extra={
                "org_id": str(organization_id),
                "project_id": str(project.id),
                "project_name": payload.name,
                "created_by": str(user_id),
            },
        )

        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description or "",
            created_by=project.created_by,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    # ── Read ────────────────────────────────────────────────────────────────

    async def get_project(
        self,
        organization_id: UUID,
        project_id: UUID,
    ) -> ProjectResponse:
        """Get a single project by ID.

        Raises:
            NotFoundError: If the project does not exist.
        """
        project = await self._repo.get_by_id(organization_id, project_id)
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )
        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description or "",
            created_by=project.created_by,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    async def list_projects(
        self,
        organization_id: UUID,
        user_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProjectResponse]:
        """List non-archived projects in an organisation that the user belongs to.

        Args:
            organization_id: Tenant scope.
            user_id: The authenticated user — only projects where this user
                is a member are returned.
            limit: Maximum results per page (capped at 200).
            offset: Number of results to skip.
        """
        projects = await self._repo.list(
            organization_id=organization_id,
            user_id=user_id,
            limit=limit,
            offset=offset,
        )
        return [
            ProjectResponse(
                id=p.id,
                name=p.name,
                description=p.description or "",
                created_by=p.created_by,
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
            for p in projects
        ]

    # ── Update ──────────────────────────────────────────────────────────────

    async def update_project(
        self,
        organization_id: UUID,
        project_id: UUID,
        payload: UpdateProjectRequest,
    ) -> ProjectResponse:
        """Update project fields (name, description).

        Raises:
            NotFoundError: If the project does not exist.
            ValidationError: If the new name conflicts with an existing project.
        """
        if payload.name is not None:
            existing = await self._repo.get_by_name(
                organization_id=organization_id,
                name=payload.name,
            )
            if existing is not None and existing.id != project_id:
                raise ValidationError(
                    message=f"A project named '{payload.name}' already exists",
                    detail={"name": payload.name},
                )

        project = await self._repo.update(
            organization_id=organization_id,
            project_id=project_id,
            name=payload.name,
            description=payload.description,
        )
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )

        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description or "",
            created_by=project.created_by,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    # ── Archive ─────────────────────────────────────────────────────────────

    async def archive_project(
        self,
        organization_id: UUID,
        project_id: UUID,
    ) -> None:
        """Archive a project (soft-delete, preserves data).

        Raises:
            NotFoundError: If the project does not exist.
        """
        project = await self._repo.archive(organization_id, project_id)
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )

        logger.info(
            "project_service.project_archived",
            extra={
                "org_id": str(organization_id),
                "project_id": str(project_id),
            },
        )

    # ── Members ─────────────────────────────────────────────────────────────

    async def add_member(
        self,
        project_id: UUID,
        payload: AddMemberRequest,
    ) -> ProjectMemberResponse:
        """Add a user to a project with the specified role.

        Raises:
            ValidationError: If the user is already a member.
        """
        existing = await self._repo.get_member(
            project_id=project_id,
            user_id=payload.user_id,
        )
        if existing is not None:
            raise ValidationError(
                message=f"User {payload.user_id} is already a member of this project",
                detail={"user_id": str(payload.user_id)},
            )

        member = await self._repo.add_member(
            project_id=project_id,
            user_id=payload.user_id,
            role=payload.role,
        )

        logger.info(
            "project_service.member_added",
            extra={
                "project_id": str(project_id),
                "user_id": str(payload.user_id),
                "role": payload.role,
            },
        )

        return ProjectMemberResponse(
            id=member.id,
            project_id=member.project_id,
            user_id=member.user_id,
            role=member.role,
            created_at=member.created_at,
        )

    async def remove_member(
        self,
        project_id: UUID,
        user_id: UUID,
    ) -> None:
        """Remove a user from a project.

        Prevents removing the last owner from a project.

        Raises:
            ValidationError: If removing the last owner.
            NotFoundError: If the membership does not exist.
        """
        # Check if this is the last owner being removed
        member = await self._repo.get_member(project_id, user_id)
        if member is None:
            raise NotFoundError(
                message=f"Membership not found for user {user_id} in project {project_id}",
                detail={"user_id": str(user_id), "project_id": str(project_id)},
            )

        if member.role == "owner":
            owner_count = await self._count_owners(project_id)
            if owner_count <= 1:
                raise ValidationError(
                    message="Cannot remove the last owner from the project",
                    detail={"project_id": str(project_id)},
                )

        removed = await self._repo.remove_member(project_id, user_id)
        if not removed:
            raise NotFoundError(
                message=f"Membership not found for user {user_id}",
                detail={"user_id": str(user_id)},
            )

        logger.info(
            "project_service.member_removed",
            extra={
                "project_id": str(project_id),
                "user_id": str(user_id),
            },
        )

    async def list_members(
        self,
        project_id: UUID,
    ) -> list[ProjectMemberResponse]:
        """List all members of a project."""
        members = await self._repo.list_members(project_id)
        return [
            ProjectMemberResponse(
                id=m.id,
                project_id=m.project_id,
                user_id=m.user_id,
                role=m.role,
                created_at=m.created_at,
            )
            for m in members
        ]

    async def update_member_role(
        self,
        project_id: UUID,
        user_id: UUID,
        role: str,
    ) -> ProjectMemberResponse:
        """Change a member's role within a project.

        Prevents removing the last owner's ownership.

        Raises:
            NotFoundError: If the membership does not exist.
            ValidationError: If downgrading the last owner.
        """
        member = await self._repo.get_member(project_id, user_id)
        if member is None:
            raise NotFoundError(
                message=f"Membership not found for user {user_id}",
                detail={"user_id": str(user_id)},
            )

        # If downgrading from owner, ensure there's at least one other owner
        if member.role == "owner" and role != "owner":
            owner_count = await self._count_owners(project_id)
            if owner_count <= 1:
                raise ValidationError(
                    message="Cannot downgrade the last owner of the project",
                    detail={"project_id": str(project_id)},
                )

        updated = await self._repo.update_member_role(
            project_id=project_id,
            user_id=user_id,
            role=role,
        )
        if updated is None:
            raise NotFoundError(
                message=f"Membership not found for user {user_id}",
                detail={"user_id": str(user_id)},
            )

        return ProjectMemberResponse(
            id=updated.id,
            project_id=updated.project_id,
            user_id=updated.user_id,
            role=updated.role,
            created_at=updated.created_at,
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _count_owners(self, project_id: UUID) -> int:
        """Count how many owners a project has."""
        members = await self._repo.list_members(project_id)
        return sum(1 for m in members if m.role == "owner")
