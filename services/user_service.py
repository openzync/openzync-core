"""User service — all business logic for the User domain.

Separation: service orchestrates, repository queries.
No SQLAlchemy expressions in this file — zero imports from ``sqlalchemy``.

Key patterns:
- Get-or-create with IntegrityError retry for concurrent-creation safety.
- Metadata deep-merge on update (delegated to repository).
- Soft-delete with GDPR purge scheduling (stub for Phase 2).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.events import EventType
from core.exceptions import ConflictError, NotFoundError, ValidationError
from repositories.user_repository import UserRepository
from schemas.users import UserListResponse, UserResponse, UserResponseWithStats
from services.webhook_service import WebhookService

# ╠ This file contains NO SQLAlchemy expressions.
# ╠ If you see a ``select()`` or ``where()``, it belongs in the repository.


class UserService:
    """Business logic for user management.

    Service methods orchestrate: validate input -> check constraints ->
    delegate to repository -> transform to response schema.

    Args:
        repo: The :class:`UserRepository` instance for DB access.
    """

    def __init__(
        self,
        repo: UserRepository,
        webhook_service: WebhookService | None = None,
    ) -> None:
        self._repo = repo
        self._webhook_service = webhook_service

    # ── Create ──────────────────────────────────────────────────────────────

    async def create_user(
        self,
        organization_id: UUID,
        external_id: str,
        name: str | None = None,
        email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserResponse:
        """Create a new user within an organization.

        Args:
            organization_id: Tenant scope.
            external_id: Caller-defined unique user identifier.
            name: Optional display name.
            email: Optional email address.
            metadata: Optional JSON metadata.

        Returns:
            A :class:`UserResponse` for the newly created user.

        Raises:
            ConflictError: A user with this ``external_id`` already exists
                in the organization.
        """

        exists = await self._repo.exists_by_external_id(
            organization_id, external_id
        )
        if exists:
            raise ConflictError(
                f"User with external_id '{external_id}' already exists "
                f"in organization {organization_id}"
            )

        user = await self._repo.create(
            organization_id=organization_id,
            external_id=external_id,
            name=name,
            email=email,
            metadata=metadata,
        )

        if self._webhook_service:
            await self._webhook_service.emit(
                organization_id=organization_id,
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
            "organization_id": user.organization_id,
            "external_id": user.external_id,
            "name": user.name,
            "email": user.email,
            "metadata": dict(user.metadata_) if user.metadata_ else {},
            "is_active": user.is_active,
            "is_deleted": user.is_deleted,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
        }

    # ── Get-or-Create ───────────────────────────────────────────────────────

    async def get_or_create_user(
        self,
        organization_id: UUID,
        external_id: str,
        name: str | None = None,
        email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserResponse:
        """Retrieve an existing user by ``external_id`` or create one.

        This is the primary entry point for memory ingestion. Callers do
        not need to pre-create users before sending messages.

        Thread-safe via the ``(organization_id, external_id)`` unique
        constraint: if two concurrent calls race, one will raise
        ``IntegrityError``; this method catches it and refetches.

        Args:
            organization_id: Tenant scope.
            external_id: Caller-defined unique user identifier.
            name: Optional display name (used only when creating).
            email: Optional email (used only when creating).
            metadata: Optional metadata (used only when creating).

        Returns:
            A :class:`UserResponse` — either pre-existing or newly created.

        Raises:
            NotFoundError: If the IntegrityError path somehow cannot find
                the row (should never happen — indicates DB inconsistency).
        """

        # Fast path: user already exists
        user = await self._repo.get_by_external_id(
            organization_id, external_id
        )
        if user is not None:
            return UserResponse.model_validate(self._user_to_dict(user))

        # Race: concurrent create — DB constraint is the source of truth
        from sqlalchemy.exc import IntegrityError

        is_new = False
        try:
            user = await self._repo.create(
                organization_id=organization_id,
                external_id=external_id,
                name=name,
                email=email,
                metadata=metadata,
            )
            is_new = True
        except IntegrityError:
            # Concurrent insert won. Rollback stale tx, then re-fetch.
            await self._repo.rollback()
            user = await self._repo.get_by_external_id(
                organization_id, external_id
            )
            if user is None:
                # Should never happen — the IntegrityError proves the
                # row exists
                raise NotFoundError(
                    f"Failed to get-or-create user '{external_id}' — "
                    f"IntegrityError was raised but no matching row "
                    f"was found."
                )

        if is_new and self._webhook_service:
            await self._webhook_service.emit(
                organization_id=organization_id,
                event_type=EventType.USER_CREATED,
                payload={
                    "user_id": str(user.id),
                    "external_id": external_id,
                },
            )

        return UserResponse.model_validate(self._user_to_dict(user))

    # ── Get ─────────────────────────────────────────────────────────────────

    async def get_user(
        self, organization_id: UUID, user_id: UUID
    ) -> UserResponseWithStats:
        """Get a user by internal UUID with aggregate statistics.

        Args:
            organization_id: Tenant scope (must match the user's org).
            user_id: The internal OpenZep user UUID.

        Returns:
            A :class:`UserResponseWithStats` with profile + counts.

        Raises:
            NotFoundError: No user with this UUID (or the user is
                soft-deleted).
        """

        user = await self._repo.get_by_uuid(organization_id, user_id)
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
        organization_id: UUID,
        user_id: UUID,
        update_fields: dict[str, Any],
    ) -> UserResponse:
        """Update user fields using the new sentinel-safe pattern.

        The router passes only explicitly-set fields via
        ``model_dump(exclude_unset=True)``. A key in ``update_fields``
        with value ``None`` means "set to null" (clear the field).
        An absent key means "do not update".

        Args:
            organization_id: Tenant scope (must match the user's org).
            user_id: The internal OpenZep user UUID.
            update_fields: Only the fields the client explicitly set.
                Valid keys: ``name``, ``email``, ``metadata``.

        Returns:
            The updated :class:`UserResponse`.

        Raises:
            NotFoundError: No user with this UUID in this organization.
        """

        user = await self._repo.update(
            organization_id=organization_id,
            user_id=user_id,
            update_fields=update_fields,
        )
        if user is None:
            raise NotFoundError(f"User {user_id} not found in organization {organization_id}")

        return UserResponse.model_validate(self._user_to_dict(user))

    # ── Delete ──────────────────────────────────────────────────────────────

    async def delete_user(
        self, organization_id: UUID, user_id: UUID
    ) -> None:
        """Soft-delete a user.

        Immediately sets ``is_deleted = True`` (user becomes invisible to
        GET/list queries). Enqueues a GDPR purge worker task that will
        hard-delete after the configured delay (default 30 days).

        Args:
            organization_id: Tenant scope (must match the user's org).
            user_id: The internal OpenZep user UUID.

        Raises:
            NotFoundError: No user with this UUID in this organization.

        .. todo::
            Phase 2 — Wire up the ARQ/worker GDPR purge task:
            ``from workers.gdpr_jobs import schedule_user_purge``
        """
        user = await self._repo.soft_delete(
            organization_id=organization_id, user_id=user_id
        )
        if user is None:
            raise NotFoundError(
                f"User {user_id} not found in organization {organization_id}"
            )

        # TODO(phase2): Enqueue GDPR purge task for 30 days later
        # from workers.gdpr_jobs import schedule_user_purge
        # await schedule_user_purge(user_id, delay_days=30)
        # See docs/implementation/07-user-session-mgmt/03-gdpr-compliance.md

    # ── List ────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        organization_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        search: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> UserListResponse:
        """List users with cursor-based pagination and optional filters.

        Args:
            organization_id: Tenant scope.
            limit: Max results per page (1-200).
            cursor: Opaque pagination token from previous response.
            search: Fuzzy match against external_id, name, email, metadata.
            created_after: Only users created on or after this timestamp.
            created_before: Only users created before this timestamp.

        Returns:
            A :class:`UserListResponse` with the current page.

        Raises:
            ValidationError: If ``limit`` is outside the 1-200 range.
        """

        if limit < 1 or limit > 200:
            raise ValidationError("limit must be between 1 and 200")

        users, next_cursor = await self._repo.list(
            organization_id=organization_id,
            limit=limit,
            cursor=cursor,
            search=search,
            created_after=created_after,
            created_before=created_before,
        )

        return UserListResponse(
            data=[UserResponse.model_validate(self._user_to_dict(u)) for u in users],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )
