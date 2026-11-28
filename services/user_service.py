"""User service — all business logic for the User domain.

Separation: service orchestrates, repository queries.
No SQLAlchemy expressions in this file — only ``sqlalchemy.exc`` for
IntegrityError → 409 mapping.

Key patterns:
- Get-or-create with IntegrityError retry for concurrent-creation safety.
- Metadata deep-merge on update (delegated to repository).
- Soft-delete with GDPR purge scheduling (stub for Phase 2).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.events import EventType
from core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from core.rbac import (
    MEMBER_DEFAULT_PERMISSIONS,
    invalidate_permissions,
    invalidate_role,
)
from core.rls import rls_bypass
from core.sorting import SortSpec
from models.user import User
from repositories.user_repository import UserRepository
from schemas.users import UserListResponse, UserResponse, UserResponseWithStats
from services.webhook_service import WebhookService

from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

logger = logging.getLogger(__name__)

# ╠ This file contains NO SQLAlchemy expressions.
# ╠ Only ``sqlalchemy.exc.IntegrityError`` is caught (→ 409 mapping).
# ╠ If you see a ``select()`` or ``where()``, it belongs in the repository.


class UserService:
    """Business logic for user management.

    Service methods orchestrate: validate input -> check constraints ->
    delegate to repository -> transform to response schema.

    Args:
        repo: The :class:`UserRepository` instance for DB access.
        webhook_service: Optional webhook emitter for user lifecycle events.
        redis: Async Redis client — used to invalidate cached roles on
            role change / deletion.  ``None`` skips invalidation (logged).
    """

    def __init__(
        self,
        repo: UserRepository,
        webhook_service: WebhookService | None = None,
        redis: AsyncRedis | None = None,  # noqa: F821
    ) -> None:
        self._repo = repo
        self._webhook_service = webhook_service
        self._redis = redis

    async def _invalidate_role_cache(self, user_id: UUID) -> None:
        """Invalidate the cached role for a user after a role-affecting change.

        Args:
            user_id: The user whose cached role must be dropped.
        """
        if self._redis is None:
            logger.error(
                "user_service.role_cache_missing_redis",
                extra={"user_id": str(user_id)},
            )
            return
        await invalidate_role(self._redis, user_id)

    async def _invalidate_permissions_cache(
        self, user_id: UUID) -> None:
        """Invalidate the cached permission set for a user after a change.

        Args:
            user_id: The user whose cached permissions must be dropped.
        """
        if self._redis is None:
            logger.error(
                "user_service.perms_cache_missing_redis",
                extra={"user_id": str(user_id)},
            )
            return
        await invalidate_permissions(self._redis, user_id)

    @staticmethod
    def _permissions_for_role(role: str | None) -> list[str]:
        """Seed permissions for a newly created user based on their role.

        Admin → empty array (wildcard via role).  Member (or
        unset) → member defaults.

        Args:
            role: The user's dashboard role.

        Returns:
            The permission list to persist.
        """
        if role == "admin":
            return []
        return list(MEMBER_DEFAULT_PERMISSIONS)

    # ── Create ──────────────────────────────────────────────────────────────

    async def create_user(
        self,
        user_id: UUID,
        external_id: str,
        name: str | None = None,
        email: str | None = None,
        metadata: dict[str, Any] | None = None,
        role: str = "member",
    ) -> UserResponse:
        """Create a new user in the platform user scope.

        Args:
            user_id: Self-ID user scope (singleton platform user).
            external_id: Caller-defined unique user identifier.
            name: Optional display name.
            email: Optional email address.
            metadata: Optional JSON metadata.
            role: Dashboard role — ``member`` only (``admin`` creation is
                frozen — raises 422; grant permissions instead).

        Returns:
            A :class:`UserResponse` for the newly created user.

        Raises:
            ValidationError: If ``role`` is ``admin`` (frozen).
            ConflictError: A user with this ``external_id`` already exists
                in the user scope.
        """
        if role == "admin":
            raise ValidationError(
                "Admin creation is frozen — create a member and grant "
                "permissions instead."
            )
        # Same bypass safety as the invite flow — exists/create must see
        # past RLS even when the session already carries an admin bypass.
        async with rls_bypass(self._repo.session):
            exists = await self._repo.exists_by_external_id(
                external_id
            )
            if exists:
                raise ConflictError(
                    f"User with external_id '{external_id}' already exists "
                    f"in user scope {user_id}"
                )

            user = await self._repo.create(

                external_id=external_id,
                name=name,
                email=email,
                metadata=metadata,
                role=role,
                permissions=self._permissions_for_role(role),
            )

        if self._webhook_service:
            await self._webhook_service.emit(
                user_id=user_id,
                event_type=EventType.USER_CREATED,
                payload={
                    "user_id": str(user.id),
                    "external_id": external_id,
                },
            )

        return UserResponse.model_validate(self._user_to_dict(user))

    def _user_to_dict(self, user: Any) -> dict[str, Any]:
        """Convert a User ORM object to a dict suitable for Pydantic validation.

        Handles the ``metadata_`` → ``metadata`` naming convention (SQLAlchemy
        reserves ``metadata`` for its own ``DeclarativeBase``).
        """
        return {
            "id": user.id,
            "user_id": user.id,
            "external_id": user.external_id,
            "name": user.name,
            "email": user.email,
            "metadata": dict(user.metadata_) if user.metadata_ else {},
            "role": user.role,
            "permissions": list(user.permissions or []),
            "is_active": user.is_active,
            "is_deleted": user.is_deleted,
            "is_pending_invite": user.invite_token_hash is not None,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
        }

    # ── Get ─────────────────────────────────────────────────────────────────

    async def get_user(
        self, user_id: UUID
    ) -> UserResponseWithStats:
        """Get a user by internal UUID with aggregate statistics.

        Args:

            user_id: The internal OpenZync user UUID.

        Returns:
            A :class:`UserResponseWithStats` with profile + counts.

        Raises:
            NotFoundError: No user with this UUID (or the user is
                soft-deleted).
        """
        user = await self._repo.get_by_uuid(user_id)
        if user is None or user.is_deleted:
            raise NotFoundError(f"User {user_id} not found")

        stats = await self._repo.get_stats(user_id)
        response = UserResponseWithStats.model_validate(self._user_to_dict(user))
        response.message_count = stats["message_count"]
        response.fact_count = stats["fact_count"]
        response.session_count = stats["session_count"]
        return response

    # ── Update ──────────────────────────────────────────────────────────────

    async def update_user(
        self,

        user_id: UUID,
        update_fields: dict[str, Any],
        *,
        actor_user_id: UUID,
        actor_permissions: frozenset[str] | None = None,
        is_admin: bool = False,
    ) -> UserResponse:
        """Update user fields using the new sentinel-safe pattern.

        The router passes only explicitly-set fields via
        ``model_dump(exclude_unset=True)``. A key in ``update_fields``
        with value ``None`` means "set to null" (clear the field).
        An absent key means "do not update".

        ``permissions`` may only be changed by a platform admin
        (``is_admin=True``, resolved DB-side by the router) — any other
        actor attempting it gets a 403.  Non-privileged self-updates
        (name/email/metadata) remain allowed.

        ``role`` changes are disabled entirely (422): the ``admin`` role
        is frozen — grant ``permissions`` instead.

        When ``permissions`` is provided, it replaces the current set
        (grants survive a role change — a role change alone never touches
        the permission array).  ``actor_permissions`` caps the grant at the
        actor's own set (never escalate); ``None`` means the actor is
        admin (wildcard — uncapped).  The cached permission set
        is invalidated after any role or permissions change.

        Args:

            user_id: The internal OpenZync user UUID.
            update_fields: Only the fields the client explicitly set.
                Valid keys: ``name``, ``email``, ``metadata``,
                ``permissions``.
            actor_user_id: The authenticated user performing the update.
            actor_permissions: The actor's grantable permission set,
                resolved by the ``get_actor_permissions`` router dependency.
                ``None`` = wildcard (admin), no cap applied.
            is_admin: Whether the actor is a DB-verified platform
                admin.  Required to change ``role``/``permissions``.

        Returns:
            The updated :class:`UserResponse`.

        Raises:
            AuthorizationError: If a non-admin attempts to change
                ``permissions`` (→ 403).
            ValidationError: If ``role`` is present (role changes are
                disabled) or the actor changes their own permissions.
            NotFoundError: No user with this UUID in this user.
        """
        if ("role" in update_fields or "permissions" in update_fields) and not is_admin:
            raise AuthorizationError(
                "Only a platform admin may change role or permissions."
            )
        if "role" in update_fields:
            raise ValidationError(
                "Role changes are disabled — grant permissions instead."
            )

        if "permissions" in update_fields:
            if user_id == actor_user_id:
                raise ValidationError("You cannot change your own permissions.")
            if actor_permissions is not None:
                requested = set(update_fields["permissions"] or [])
                # Cap the grant at the actor's own set — never escalate.
                update_fields["permissions"] = sorted(
                    requested & set(actor_permissions)
                )

        try:
            user = await self._repo.update(

                user_id=user_id,
                update_fields=update_fields,
            )
        except IntegrityError:
            # Lost the partial-unique-email race — same 409 as the router
            # pre-checks, never a raw 500.
            from core.exceptions import ConflictError

            raise ConflictError("Email is already in use.") from None
        if user is None:
            raise NotFoundError(f"User {user_id} not found in user scope {user_id}")

        if "permissions" in update_fields:
            await self._invalidate_permissions_cache(user_id)

        return UserResponse.model_validate(self._user_to_dict(user))

    # ── Platform admin (role change) ─────────────────────────────────────────

    async def update_member_role(
        self,
        user_id: UUID,
        role: str,
    ) -> User:
        """Set a member's role (platform admin surface).

        The ``admin`` role is frozen: any request with ``role == "admin"``
        raises 422, even from root — grant ``permissions`` instead.  Only
        ``member`` (demotion) is assignable.  The root user's own role is
        frozen (422), and demoting the last active admin raises 422.
        Invalidates the cached role/permissions so RBAC cannot outlive
        the change.

        Args:
            user_id: The member's user UUID.
            role: The new role (only ``member`` — ``admin`` raises 422).

        Returns:
            The updated User.

        Raises:
            ValidationError: If ``role`` is ``admin``, the target is root,
                or the last admin is demoted.
            NotFoundError: If the user does not exist.
        """
        target = await self._repo.get_by_uuid(user_id)
        if target is None:
            raise NotFoundError(
                f"User {user_id} not found in user scope {user_id}."
            )
        if target.external_id == "root":
            raise ValidationError(
                "The root user's role cannot be changed via this endpoint."
            )
        if role == "admin":
            # The admin role is frozen — no promotion path, even for root.
            raise ValidationError(
                "Admin role is frozen — grant permissions instead."
            )
        if target.role == "admin" and role != "admin":
            admin_count = await self._repo.count_active_admins()
            if admin_count <= 1:
                raise ValidationError(
                    "Cannot demote the last admin in the user scope."
                )
        update_fields: dict[str, Any] = {"role": role}
        user = await self._repo.update(

            user_id=user_id,
            update_fields=update_fields,
        )
        if user is None:
            raise NotFoundError(
                f"User {user_id} not found in user scope {user_id}."
            )
        await self._invalidate_role_cache(user_id)
        await self._invalidate_permissions_cache(user_id)
        return user

    # ── Delete ──────────────────────────────────────────────────────────────

    async def delete_user(
        self,

        user_id: UUID,
        *,
        actor_user_id: UUID,
    ) -> None:
        """Soft-delete a user.

        Immediately sets ``is_deleted = True`` (user becomes invisible to
        GET/list queries). Enqueues a GDPR purge worker task that will
        hard-delete after the configured delay (default 30 days).

        Guards (in order): you cannot delete your own account (422),
        the root account cannot be deleted via this endpoint (422), and
        the last active admin of a user scope cannot be deleted (422).
        The deleted user's cached role is invalidated.

        Args:

            user_id: The internal OpenZync user UUID.
            actor_user_id: The authenticated user performing the deletion.

        Raises:
            ValidationError: If the actor deletes their own account, the
                target is root, or the target is the last admin.
            NotFoundError: No user with this UUID in this user.

        .. todo::
            Phase 2 — Wire up the ARQ/worker GDPR purge task:
            ``from workers.gdpr_jobs import schedule_user_purge``
        """
        if user_id == actor_user_id:
            raise ValidationError("You cannot delete your own account.")

        user = await self._repo.get_by_uuid(user_id)
        if user is None:
            raise NotFoundError(
                f"User {user_id} not found in user scope {user_id}"
            )
        if user.external_id == "root":
            raise ValidationError(
                "The root account cannot be deleted via this endpoint."
            )
        if user.role == "admin":
            admin_count = await self._repo.count_active_admins()
            # ⚠️ Same TOCTOU window as update_user's demotion guard — two
            # concurrent deletes of different admins (scope has exactly 2)
            # could both pass, leaving zero admins.
            if admin_count <= 1:
                raise ValidationError(
                    "Cannot delete the last admin in the user scope."
                )

        deleted = await self._repo.soft_delete(
            user_id,
        )
        if deleted is None:
            raise NotFoundError(
                f"User {user_id} not found in user scope {user_id}"
            )

        await self._invalidate_role_cache(user_id)
        await self._invalidate_permissions_cache(user_id)

        # TODO(phase2): Enqueue GDPR purge task for 30 days later
        # from workers.gdpr_jobs import schedule_user_purge
        # await schedule_user_purge(user_id, delay_days=30)
        # See docs/implementation/07-user-session-mgmt/03-gdpr-compliance.md

    # ── List ────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        user_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        search: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        sort: SortSpec | None = None,
    ) -> UserListResponse:
        """List users with cursor-based pagination and optional filters.

        Args:
            user_id: Tenant scope.
            limit: Max results per page (1-200).
            cursor: Opaque pagination token from previous response.
            search: Fuzzy match against external_id, name, email, metadata.
            created_after: Only users created on or after this timestamp.
            created_before: Only users created before this timestamp.
            sort: Validated sort spec (forwarded opaque to the repository).

        Returns:
            A :class:`UserListResponse` with the current page.

        Raises:
            ValidationError: If ``limit`` is outside the 1-200 range.
        """
        if limit < 1 or limit > 200:
            raise ValidationError("limit must be between 1 and 200")

        users, next_cursor = await self._repo.list(
            user_id=user_id,
            limit=limit,
            cursor=cursor,
            search=search,
            created_after=created_after,
            created_before=created_before,
            sort=sort,
        )

        return UserListResponse(
            data=[UserResponse.model_validate(self._user_to_dict(u)) for u in users],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )
