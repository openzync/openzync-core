"""Project service — orchestrates project and project member lifecycle.

Handles creation, discovery, membership management, and archiving of
project workspaces.  All methods are scoped to an ``user_id``
for tenant isolation.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.exceptions import NotFoundError, ValidationError
from core.sorting import SortSpec
from repositories.project_pin_repository import ProjectPinRepository
from repositories.project_repository import ProjectRepository
from schemas.projects import (
    CreateProjectRequest,
    ProjectMemberResponse,
    ProjectResponse,
    UpdateProjectRequest,
)
from schemas.users import UserResponse

logger = logging.getLogger(__name__)

MAX_PINS = 3
"""Maximum pinned projects per user per organization."""


class ProjectService:
    """Orchestrates project and member lifecycle operations.

    Args:
        repo: The project repository instance (request-scoped).
        pin_repo: The project-pin repository instance (request-scoped).
    """

    def __init__(self, repo: ProjectRepository, pin_repo: ProjectPinRepository) -> None:
        self._repo = repo
        self._pin_repo = pin_repo

    # ── Create ──────────────────────────────────────────────────────────────

    async def create_project(
        self,

        user_id: UUID | None,
        payload: CreateProjectRequest,
    ) -> ProjectResponse:
        """Create a new project and optionally add the creator as an owner.

        When ``user_id`` is ``None`` (API-key-authenticated request), the
        project is created without a ``created_by`` attribution and without
        an initial owner member.  Owner management must be handled through
        the dashboard.

        Args:

            user_id: The creating user (becomes the initial owner), or
                ``None`` for API-key-authenticated requests.
            payload: Name, optional description, and optional metadata.

        Returns:
            The created ProjectResponse.

        Raises:
            ValidationError: If a project with the same name already exists
                in this organisation.
        """
        # Check for duplicate names within the org
        existing = await self._repo.get_by_name(
            user_id=user_id,
            name=payload.name,
        )
        if existing is not None:
            raise ValidationError(
                message=(
                    f"A project named '{payload.name}' already exists "
                    "in this organisation"
                ),
                detail={"name": payload.name},
            )

        project = await self._repo.create(
            user_id=user_id,
            name=payload.name,
            created_by=user_id,
            description=payload.description,
            metadata_=payload.metadata,
        )

        # Add creator as active owner (skipped for API-key requests with
        # no user context) — the creator path bypasses invite approval.
        if user_id is not None:
            await self._repo.add_member(
                project_id=project.id,
                user_id=user_id,
                role="owner",
                status="active",
            )

        logger.info(
            "project_service.project_created",
            extra={
                "user_id": str(user_id),
                "project_id": str(project.id),
                "project_name": payload.name,
                "created_by": str(user_id) if user_id else None,
            },
        )

        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description or "",
            created_by=project.created_by,
            member_count=1 if user_id is not None else 0,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    # ── Read ────────────────────────────────────────────────────────────────

    async def get_project(
        self,
        user_id: UUID,
        project_id: UUID,
    ) -> ProjectResponse:
        """Get a single project by ID.

        Raises:
            NotFoundError: If the project does not exist.
        """
        project = await self._repo.get_by_id(user_id, project_id)
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )
        member_count = await self._repo.count_members(project_id)
        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description or "",
            created_by=project.created_by,
            member_count=member_count,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    async def list_projects(
        self,

        user_id: UUID | None,
        limit: int = 50,
        offset: int = 0,
        pinned_only: bool = False,
        sort: SortSpec | None = None,
        include_archived: bool = False,
    ) -> list[ProjectResponse]:
        """List projects in an organisation.

        When ``user_id`` is provided, only projects where the user is a
        member are returned.  When ``user_id`` is ``None`` (API key auth),
        all projects in the org are returned with
        ``is_pinned=False``.  Each entry carries ``is_pinned`` for the
        requesting user; ``pinned_only=True`` returns just their pins,
        most recently pinned first.

        Archived projects are excluded by default; pass
        ``include_archived=True`` to include them.  The ``pinned_only``
        branch always excludes archived projects — archiving deletes a
        project's pins, so no archived project can be pinned.

        Default ``created_at/desc`` (``pinned_at/desc`` for pinned_only);
        whitelist ``name``, ``created_at``, ``updated_at`` (plus
        ``pinned_at`` when ``pinned_only``).

        Args:

            user_id: The authenticated user's UUID, or ``None`` for
                API-key-authenticated requests.
            limit: Maximum results per page (capped at 200).
            offset: Number of results to skip.
            pinned_only: Return only the user's pinned projects.
            sort: Validated sort spec (forwarded opaque to the repository).
            include_archived: If ``True``, include archived projects.

        Raises:
            ValidationError: If ``pinned_only`` is set without a user
                (API-key callers have no pins).
        """
        if pinned_only and user_id is None:
            raise ValidationError(
                message="pinned_only requires dashboard authentication",
                detail={"pinned_only": pinned_only},
            )

        pinned_ids: set[UUID] = set()
        if user_id is not None:
            pinned_ids = await self._pin_repo.get_pinned_ids(user_id)
            if pinned_only:
                projects = await self._pin_repo.list_pinned_projects(

                    user_id=user_id,
                    limit=limit,
                    offset=offset,
                    sort=sort,
                )
                counts = await self._repo.count_members_for_projects(
                    [p.id for p in projects]
                )
                return [
                    ProjectResponse(
                        id=p.id,
                        name=p.name,
                        description=p.description or "",
                        created_by=p.created_by,
                        member_count=counts.get(p.id, 0),
                        is_pinned=True,
                        created_at=p.created_at,
                        updated_at=p.updated_at,
                    )
                    for p in projects
                ]

        projects = await self._repo.list(

            user_id=user_id,
            limit=limit,
            offset=offset,
            sort=sort,
            include_archived=include_archived,
        )
        # Batch-load member counts for all projects in a single query
        project_ids = [p.id for p in projects]
        counts = await self._repo.count_members_for_projects(project_ids)
        return [
            ProjectResponse(
                id=p.id,
                name=p.name,
                description=p.description or "",
                created_by=p.created_by,
                member_count=counts.get(p.id, 0),
                is_pinned=(p.id in pinned_ids),
                is_archived=p.is_archived,
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
            for p in projects
        ]

    # ── Update ──────────────────────────────────────────────────────────────

    async def update_project(
        self,
        user_id: UUID,
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
                user_id=user_id,
                name=payload.name,
            )
            if existing is not None and existing.id != project_id:
                raise ValidationError(
                    message=f"A project named '{payload.name}' already exists",
                    detail={"name": payload.name},
                )

        project = await self._repo.update(

            project_id=project_id,
            name=payload.name,
            description=payload.description,
        )
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )

        member_count = await self._repo.count_members(project_id)
        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description or "",
            created_by=project.created_by,
            member_count=member_count,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    # ── Archive ─────────────────────────────────────────────────────────────

    async def archive_project(
        self,
        user_id: UUID,
        project_id: UUID,
    ) -> None:
        """Archive a project (soft-delete, preserves data).

        Raises:
            NotFoundError: If the project does not exist.
        """
        project = await self._repo.archive(user_id, project_id)
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )

        freed = await self._pin_repo.delete_for_project(project_id)

        logger.info(
            "project_service.project_archived",
            extra={
                "user_id": str(user_id),
                "project_id": str(project_id),
                "pins_removed": freed,
            },
        )

    async def unarchive_project(
        self,
        user_id: UUID,
        project_id: UUID,
    ) -> ProjectResponse:
        """Restore an archived project.

        The repository fetches via a direct select including archived
        rows — ``get_by_id`` 404s archived projects so it cannot be
        used here.

        Args:
            user_id: Tenant scope.
            project_id: The project to restore.

        Returns:
            The restored ProjectResponse.

        Raises:
            NotFoundError: If the project does not exist.
        """
        project = await self._repo.unarchive(user_id, project_id)
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )

        member_count = await self._repo.count_members(project_id)
        logger.info(
            "project_service.project_unarchived",
            extra={
                "user_id": str(user_id),
                "project_id": str(project_id),
            },
        )
        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description or "",
            created_by=project.created_by,
            member_count=member_count,
            is_archived=project.is_archived,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    # ── Pins ──────────────────────────────────────────────────────────────────

    async def pin_project(
        self,

        user_id: UUID,
        project_id: UUID,
    ) -> None:
        """Pin a project for a user (idempotent).

        Guards, in order: project exists and is unarchived, caller is a
        member, already pinned (no-op), pin limit (``MAX_PINS``).

        Args:

            user_id: The user pinning the project.
            project_id: The project to pin.

        Raises:
            NotFoundError: If the project does not exist, is archived,
                or the user is not a member.
            ValidationError: If the user already has ``MAX_PINS`` pins.
        """
        project = await self._repo.get_by_id(user_id, project_id)
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )
        if project.is_archived:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )
        member = await self._repo.get_member(project_id, user_id)
        if member is None:
            raise NotFoundError(
                message=f"Membership not found for user {user_id}",
                detail={"user_id": str(user_id)},
            )
        if await self._pin_repo.is_pinned(user_id, project_id):
            return
        pinned = await self._pin_repo.pin_if_under_limit(
            user_id, project_id, MAX_PINS
        )
        if pinned is None:
            raise ValidationError(
                message=f"Pin limit reached (max {MAX_PINS} pinned projects)",
                detail={"limit": MAX_PINS},
            )

        logger.info(
            "project_service.project_pinned",
            extra={
                "user_id": str(user_id),
                "project_id": str(project_id),
            },
        )

    async def unpin_project(
        self,

        user_id: UUID,
        project_id: UUID,
    ) -> None:
        """Remove a user's pin on a project (no-op if absent).

        Args:

            user_id: The user unpinning the project.
            project_id: The project to unpin.
        """
        await self._pin_repo.unpin(user_id, project_id)

        logger.info(
            "project_service.project_unpinned",
            extra={
                "user_id": str(user_id),
                "project_id": str(project_id),
            },
        )

    # ── Members ─────────────────────────────────────────────────────────────

    async def remove_member(
        self,
        project_id: UUID,
        user_id: UUID,
    ) -> None:
        """Remove a user from a project, or revoke their pending invite.

        Pending rows revoke unconditionally (a pending owner holds no
        effective ownership). Prevents removing the last active owner.

        Raises:
            ValidationError: If removing the last owner.
            NotFoundError: If the membership does not exist.
        """
        # Check if this is the last owner being removed
        member = await self._repo.get_membership_any(project_id, user_id)
        if member is None:
            raise NotFoundError(
                message=(
                    f"Membership not found for user {user_id} in project {project_id}"
                ),
                detail={"user_id": str(user_id), "project_id": str(project_id)},
            )

        if member.status == "pending":
            removed = await self._repo.remove_member(project_id, user_id)
            if not removed:
                raise NotFoundError(
                    message=f"Membership not found for user {user_id}",
                    detail={"user_id": str(user_id)},
                )
            logger.info(
                "project_membership.member_revoked",
                extra={
                    "project_id": str(project_id),
                    "user_id": str(user_id),
                },
            )
            return

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
        sort: SortSpec | None = None,
    ) -> list[ProjectMemberResponse]:
        """List all members of a project with nested user details.

        Returns every row regardless of ``status`` (``pending`` invites
        included); only ``active`` rows grant access.

        Default ``created_at/asc``; whitelist ``created_at``, ``role``,
        ``name``, ``email``.

        Args:
            project_id: The project's UUID.
            sort: Validated sort spec (forwarded opaque to the repository).

        Returns:
            Memberships with ``user`` populated, or ``None`` when the
            user row is missing.
        """
        members = await self._repo.list_members(project_id, sort=sort)
        return [
            ProjectMemberResponse(
                id=m.id,
                project_id=m.project_id,
                user_id=m.user_id,
                role=m.role,
                status=m.status,
                created_at=m.created_at,
                user=self._to_user_response(m.user) if m.user is not None else None,
            )
            for m in members
        ]

    @staticmethod
    def _to_user_response(user: Any) -> UserResponse:
        """Map a User ORM row to UserResponse without touching ``User.metadata``.

        ``metadata`` is reserved on ``DeclarativeBase`` — the real column
        lives on ``metadata_`` — so ``model_validate(orm)`` reads
        ``MetaData()`` and 500s. Mirrors ``UserService._user_to_dict``.
        """
        return UserResponse.model_validate(
            {
                "id": user.id,
                "user_id": user.id,
                "external_id": user.external_id,
                "name": user.name,
                "email": user.email,
                "metadata": dict(user.metadata_) if user.metadata_ else {},
                "role": user.role,
                "permissions": list(user.permissions or []),
                "is_deleted": user.is_deleted,
                "is_pending_invite": user.invite_token_hash is not None,
                "created_at": user.created_at,
                "updated_at": user.updated_at,
            }
        )

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
            status=updated.status,
            created_at=updated.created_at,
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _count_owners(self, project_id: UUID) -> int:
        """Count how many ACTIVE owners a project has."""
        members = await self._repo.list_members(project_id)
        return sum(
            1 for m in members if m.role == "owner" and m.status == "active"
        )
