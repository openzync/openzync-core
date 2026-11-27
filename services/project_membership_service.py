"""Project membership service — email-based member onboarding.

Resolves an email to a user (existing active, pending invite, or new via
the registration-mode-gated invite flow) and attaches them to a project
as a ``pending`` membership. Nothing here activates membership — the
invitee approves their own invite (or a superadmin approves for them) via
:meth:`approve`. Pure orchestration: all SQL lives in the repositories,
all HTTP in the router, all invite email mechanics in :class:`InviteService`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from repositories.instance_settings_repository import InstanceSettingsRepository
from repositories.project_repository import ProjectRepository
from repositories.user_repository import UserRepository
from schemas.auth import InviteRequest
from schemas.projects import ProjectApprovalResponse, ProjectMemberResponse
from utils.pii import mask_email

if TYPE_CHECKING:
    from models.project_member import ProjectMember
    from services.invite_service import InviteService

logger = logging.getLogger(__name__)


class ProjectMembershipService:
    """Email-based project member onboarding.

    Args:
        repo: Project repository — membership reads/writes.
        user_repo: User repository — email lookup via the existing
            ``find_user_by_email`` path (no ad-hoc bypass).
        settings_repo: Instance-settings repository — registration-mode gate.
        invite_service: Invite service — pending-user creation on the
            ``open`` registration path (owns the row-first-then-email flow,
            so this service never inserts users directly).
    """

    def __init__(
        self,
        repo: ProjectRepository,
        user_repo: UserRepository,
        settings_repo: InstanceSettingsRepository,
        invite_service: InviteService,
    ) -> None:
        self._repo = repo
        self._user_repo = user_repo
        self._settings_repo = settings_repo
        self._invite_service = invite_service

    async def add_by_email(
        self,
        project_id: UUID,
        email: str,
        role: str,
        *,
        invited_by: UUID,
    ) -> ProjectMemberResponse:
        """Invite a project member by email address (always ``pending``).

        # ⚠️ BREAKING: invites no longer activate immediately — every path
        creates a ``pending`` membership the invitee must approve.

        Args:
            project_id: The project to add the member to.
            email: Invitee email (stripped/lowercased; the schema already
                normalizes, this re-normalizes for direct callers).
            role: ``"owner"`` or ``"member"``.
            invited_by: Requesting user — attributed as the inviter when a
                pending user is created on the ``open`` path.

        Returns:
            The new ``pending`` membership as ``ProjectMemberResponse``.

        Raises:
            ValidationError: Empty email, bad role, already a member
                (any status), deactivated account, or ``approval``
                registration mode.
            ConflictError: Invite already pending for this email or project
                membership, or a concurrent add won the race.
            AuthorizationError: ``invite_only`` registration mode.
        """
        normalized = email.strip().lower()
        if not normalized:
            raise ValidationError(
                message="Email must not be empty",
                detail={"code": "email_required"},
            )
        if role not in ("owner", "member"):
            raise ValidationError(
                message=f"Invalid role '{role}' — must be 'owner' or 'member'",
                detail={"code": "invalid_role", "role": role},
            )

        user = await self._user_repo.find_user_by_email(normalized)
        if user is not None:
            existing = await self._repo.get_membership_any(
                project_id=project_id, user_id=user.id
            )
            if existing is not None:
                if existing.status == "pending":
                    raise ConflictError(
                        message=f"An invite is already pending for {normalized}",
                        detail={
                            "code": "invite_pending",
                            "user_id": str(user.id),
                            "email": mask_email(normalized),
                        },
                    )
                raise ValidationError(
                    message=f"User {user.id} is already a member of this project",
                    detail={"code": "already_member", "user_id": str(user.id)},
                )
            if user.invite_token_hash is not None:
                raise ConflictError(
                    message=f"An invite is already pending for {normalized}",
                    detail={
                        "code": "invite_pending",
                        "email": mask_email(normalized),
                    },
                )
            if not user.is_active:
                raise ValidationError(
                    message=f"User {normalized} is deactivated",
                    detail={
                        "code": "user_inactive",
                        "email": mask_email(normalized),
                    },
                )
            member = await self._insert_member(project_id, user.id, role)
            logger.info(
                "project_membership.member_requested",
                extra={
                    "project_id": str(project_id),
                    "user_id": str(user.id),
                    "email": mask_email(normalized),
                    "role": role,
                },
            )
            return self._to_response(member)

        match await self._settings_repo.get_registration_mode():
            case "open":
                return await self._invite_and_add(
                    project_id, normalized, role, invited_by
                )
            case "invite_only":
                raise AuthorizationError(
                    message=(
                        "Registration is invite-only — "
                        f"ask a superadmin to invite {normalized}."
                    ),
                    detail={
                        "code": "registration_invite_only",
                        "email": mask_email(normalized),
                    },
                )
            case "approval":
                raise ValidationError(
                    message=(
                        f"User {normalized} must register and wait for "
                        "approval before joining projects."
                    ),
                    detail={
                        "code": "registration_approval_required",
                        "email": mask_email(normalized),
                    },
                )
            case mode:
                # Fail closed on unknown modes (e.g. "disabled").
                raise AuthorizationError(
                    message=(
                        "Registration is currently closed — "
                        f"ask a superadmin to invite {normalized}."
                    ),
                    detail={
                        "code": "registration_closed",
                        "mode": mode,
                        "email": mask_email(normalized),
                    },
                )

    async def _invite_and_add(
        self,
        project_id: UUID,
        email: str,
        role: str,
        invited_by: UUID,
    ) -> ProjectMemberResponse:
        """Create a pending user via the invite flow, then attach them.

        Both writes share the request session, so ``get_db`` commits (or
        rolls back) them atomically.
        """
        invite = await self._invite_service.invite_user(
            admin_user_id=invited_by,
            # note: no display name on this endpoint — local part mirrors
            # the signup default (org_join).
            payload=InviteRequest(email=email, name=email.partition("@")[0] or email),
        )
        # Second audit trail for the created user (the router's
        # @audit_action only covers "project.member.add").
        logger.info(
            "project_membership.user_invited",
            extra={
                "project_id": str(project_id),
                "user_id": str(invite.id),
                "email": mask_email(email),
                "invited_by": str(invited_by),
            },
        )
        member = await self._insert_member(project_id, invite.id, role)
        logger.info(
            "project_membership.member_requested",
            extra={
                "project_id": str(project_id),
                "user_id": str(invite.id),
                "email": mask_email(email),
                "role": role,
            },
        )
        return self._to_response(member)

    async def approve(
        self,
        project_id: UUID,
        user_id: UUID,
        *,
        actor: UUID,
        is_superadmin: bool = False,
    ) -> ProjectMemberResponse:
        """Approve a pending invite, activating the membership.

        Only the invitee (``actor == user_id``) or a platform superadmin
        may approve.

        Args:
            project_id: The project holding the invite.
            user_id: The invitee whose membership activates.
            actor: The requesting user.
            is_superadmin: DB-verified platform superadmin flag (resolved
                by the router, never trusted from the JWT claim).

        Returns:
            The activated membership as ``ProjectMemberResponse``.

        Raises:
            AuthorizationError: Actor is neither the invitee nor superadmin.
            ValidationError: The membership is already active.
            NotFoundError: No membership for this user.
        """
        if actor != user_id and not is_superadmin:
            raise AuthorizationError(
                message="Only the invitee or a superadmin can approve this invite",
                detail={"code": "not_invitee", "user_id": str(user_id)},
            )
        member = await self._repo.get_membership_any(project_id, user_id)
        if member is None:
            raise NotFoundError(
                message=f"Membership not found for user {user_id}",
                detail={"user_id": str(user_id)},
            )
        if member.status == "active":
            raise ValidationError(
                message=f"User {user_id} is already an active member",
                detail={"code": "already_member", "user_id": str(user_id)},
            )
        updated = await self._repo.update_member_status(
            project_id=project_id, user_id=user_id, status="active"
        )
        # note: row existed a moment ago; None means a concurrent delete won.
        if updated is None:
            raise NotFoundError(
                message=f"Membership not found for user {user_id}",
                detail={"user_id": str(user_id)},
            )
        logger.info(
            "project_membership.member_approved",
            extra={
                "project_id": str(project_id),
                "user_id": str(user_id),
                "actor": str(actor),
            },
        )
        return self._to_response(updated)

    async def reject(
        self,
        project_id: UUID,
        user_id: UUID,
        *,
        actor: UUID,
        is_superadmin: bool = False,
    ) -> None:
        """Reject a pending invite, deleting the membership row.

        Only the invitee (``actor == user_id``) or a platform superadmin
        may reject. Revoking via ``DELETE`` (manager path) lives in
        :class:`ProjectService.remove_member`.

        Raises:
            AuthorizationError: Actor is neither the invitee nor superadmin.
            ValidationError: The membership is already active (remove it
                via ``DELETE`` instead).
            NotFoundError: No membership for this user.
        """
        if actor != user_id and not is_superadmin:
            raise AuthorizationError(
                message="Only the invitee or a superadmin can reject this invite",
                detail={"code": "not_invitee", "user_id": str(user_id)},
            )
        member = await self._repo.get_membership_any(project_id, user_id)
        if member is None:
            raise NotFoundError(
                message=f"Membership not found for user {user_id}",
                detail={"user_id": str(user_id)},
            )
        if member.status == "active":
            raise ValidationError(
                message="Membership is active — remove it via DELETE instead",
                detail={"code": "already_member", "user_id": str(user_id)},
            )
        await self._repo.remove_member(project_id, user_id)
        logger.info(
            "project_membership.member_rejected",
            extra={
                "project_id": str(project_id),
                "user_id": str(user_id),
                "actor": str(actor),
            },
        )

    async def list_pending_for_user(
        self, user_id: UUID
    ) -> list[ProjectApprovalResponse]:
        """List the user's pending invites with project names.

        Args:
            user_id: The invitee.

        Returns:
            Pending memberships, oldest first.
        """
        rows = await self._repo.list_pending_for_user(user_id)
        return [
            ProjectApprovalResponse(
                id=m.id,
                project_id=m.project_id,
                project_name=name,
                role=m.role,  # type: ignore[arg-type]
                status=m.status,  # type: ignore[arg-type]
                created_at=m.created_at,
            )
            for m, name in rows
        ]

    async def _insert_member(
        self, project_id: UUID, user_id: UUID, role: str
    ) -> ProjectMember:
        """Insert the membership row, mapping races to 409.

        Raises:
            ConflictError: A concurrent add won the unique constraint.
                The exception propagates so ``get_db`` rolls the request
                transaction back — including a just-created pending user.
                # ⚠️ RACE: the invite email was already sent at that point
                and cannot be unsent (same pre-existing gap as the admin
                invite flow); the client retries and hits ``already_member``.
        """
        try:
            return await self._repo.add_member(
                project_id=project_id, user_id=user_id, role=role
            )
        except IntegrityError as err:
            logger.warning(
                "project_membership.member_race",
                extra={
                    "project_id": str(project_id),
                    "user_id": str(user_id),
                },
            )
            raise ConflictError(
                message="This user was just added to the project",
                detail={
                    "code": "already_member",
                    "user_id": str(user_id),
                },
            ) from err

    @staticmethod
    def _to_response(member: ProjectMember) -> ProjectMemberResponse:
        """Map a fresh membership row to its response schema."""
        return ProjectMemberResponse(
            id=member.id,
            project_id=member.project_id,
            user_id=member.user_id,
            role=member.role,
            status=member.status,
            created_at=member.created_at,
        )
