"""Session service — business logic for session management.

Provides create, read, list, and delete operations for conversation
sessions.  All DB access is delegated to ``SessionRepository``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from schemas.common import PaginatedResponse
from schemas.sessions import (
    MessageResponse,
    SessionListResponse,
    SessionResponse,
)

from core.exceptions import ConflictError, NotFoundError, ValidationError
from schemas.mappers import episode_to_dict, session_to_dict, session_to_list_dict
from repositories.session_repository import SessionRepository

logger = logging.getLogger(__name__)


class SessionService:
    """Business logic for session management.

    Args:
        repo: The session repository.
    """

    def __init__(self, repo: SessionRepository) -> None:
        self._repo = repo

    # ── Create ──────────────────────────────────────────────────────────────

    async def create_session(
        self,
        organization_id: UUID,
        user_id: UUID,
        external_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionResponse:
        """Create a new session for a user.

        Args:
            organization_id: The organization UUID for tenant isolation.
            user_id: The owning user's UUID.
            external_id: Caller-defined session identifier (unique per user).
            metadata: Optional session metadata.

        Returns:
            The newly created session response.

        Raises:
            ConflictError: A session with this ``external_id`` already
                exists for the given user.
        """
        # Check for duplicates before inserting.
        existing = await self._repo.get_by_external_id(
            organization_id, user_id, external_id
        )
        if existing is not None:
            raise ConflictError(
                f"Session '{external_id}' already exists for user {user_id}"
            )

        session = await self._repo.create(
            organization_id=organization_id,
            user_id=user_id,
            external_id=external_id,
            metadata=metadata,
        )

        logger.info(
            "session.created",
            extra={
                "session_id": str(session.id),
                "user_id": str(user_id),
                "external_id": external_id,
            },
        )

        return SessionResponse.model_validate(
            session_to_dict(session, message_count=0, fact_count=0)
        )

    # ── Get ─────────────────────────────────────────────────────────────────

    async def get_session(
        self, org_id: UUID, session_id: UUID, user_id: UUID | None = None
    ) -> SessionResponse:
        """Get session by UUID with aggregate statistics.

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID.
            user_id: Optional user UUID for intra-org isolation.

        Returns:
            The session response with message/fact counts.

        Raises:
            NotFoundError: Session not found or soft-deleted.
        """
        session = await self._repo.get_by_uuid(org_id, session_id, user_id=user_id)
        if session is None:
            raise NotFoundError(f"Session {session_id} not found")

        stats = await self._repo.get_stats(session_id)

        return SessionResponse.model_validate(
            session_to_dict(
                session,
                message_count=stats["message_count"],
                fact_count=stats["fact_count"],
            )
        )

    async def get_session_by_external_id(
        self, org_id: UUID, user_id: UUID, external_id: str
    ) -> SessionResponse:
        """Get a session for a user by its external_id.

        Args:
            org_id: The organization UUID for tenant isolation.
            user_id: The owning user's UUID.
            external_id: The caller-defined session identifier.

        Returns:
            The session response with aggregate statistics.

        Raises:
            NotFoundError: Session not found or soft-deleted.
        """
        session = await self._repo.get_by_external_id(org_id, user_id, external_id)
        if session is None:
            raise NotFoundError(
                f"Session external_id={external_id!r} not found "
                f"for user {user_id}"
            )

        stats = await self._repo.get_stats(session.id)

        return SessionResponse.model_validate(
            session_to_dict(
                session,
                message_count=stats["message_count"],
                fact_count=stats["fact_count"],
            )
        )

    async def get_session_by_uuid(
        self, org_id: UUID, session_id: UUID
    ) -> SessionResponse:
        """Get a session by its internal UUID (alias for ``get_session``).

        Provided for callers that already have the UUID and don't need
        external_id resolution.

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID.

        Returns:
            The session response with aggregate statistics.

        Raises:
            NotFoundError: Session not found or soft-deleted.
        """
        return await self.get_session(org_id, session_id)

    # ── List ────────────────────────────────────────────────────────────────

    async def list_sessions(
        self,
        org_id: UUID,
        user_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        include_closed: bool = False,
    ) -> PaginatedResponse[SessionListResponse]:
        """List sessions for a user with cursor-based pagination.

        By default returns open (non-closed, non-deleted) sessions,
        excluding the ``__default__`` auto-created session.

        Args:
            org_id: The organization UUID for tenant isolation.
            user_id: The owning user's UUID.
            limit: Maximum items per page (1–200).
            cursor: Opaque base64 cursor from a previous page.
            include_closed: If ``True``, include closed sessions.

        Returns:
            A paginated response with lightweight session items.

        Raises:
            ValidationError: If ``limit`` is out of range.
        """
        if limit < 1 or limit > 200:
            raise ValidationError("limit must be between 1 and 200")

        sessions, next_cursor = await self._repo.list(
            org_id=org_id,
            user_id=user_id,
            limit=limit,
            cursor=cursor,
            include_closed=include_closed,
        )

        # Batch-load message counts — one query instead of N+1.
        session_ids = [s.id for s in sessions]
        stats = await self._repo.batch_get_stats(session_ids, org_id) if session_ids else {}

        items = [
            SessionListResponse.model_validate(
                session_to_list_dict(
                    s,
                    message_count=stats.get(s.id, {}).get("message_count", 0),
                )
            )
            for s in sessions
        ]

        return PaginatedResponse[SessionListResponse](
            data=items,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    # ── Messages ────────────────────────────────────────────────────────────

    async def get_messages(
        self,
        org_id: UUID,
        session_id: UUID,
        limit: int = 100,
        cursor: str | None = None,
        user_id: UUID | None = None,
    ) -> PaginatedResponse[MessageResponse]:
        """Get paginated messages for a session.

        Messages are ordered by ``sequence_number`` for deterministic,
        tie-free ordering.

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID.
            limit: Maximum items per page (1–500).
            cursor: Opaque base64 cursor from a previous page.
            user_id: Optional user UUID for intra-org isolation.

        Returns:
            A paginated response with message items.

        Raises:
            NotFoundError: If the session does not exist.
            ValidationError: If ``limit`` is out of range.
        """
        if limit < 1 or limit > 500:
            raise ValidationError("limit must be between 1 and 500")

        # Verify the session exists before fetching messages.
        session = await self._repo.get_by_uuid(org_id, session_id, user_id=user_id)
        if session is None:
            raise NotFoundError(f"Session {session_id} not found")

        messages, next_cursor = await self._repo.get_messages(
            org_id=org_id,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
        )

        items = [
            MessageResponse.model_validate(episode_to_dict(m))
            for m in messages
        ]

        return PaginatedResponse[MessageResponse](
            data=items,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    # ── Delete ──────────────────────────────────────────────────────────────

    async def delete_session(
        self, org_id: UUID, session_id: UUID, user_id: UUID | None = None
    ) -> None:
        """Soft-delete a session, scoped to org and optionally user.

        Episodes are unlinked (``session_id`` set to ``NULL``) but
        preserved as orphaned history for audit purposes.

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID.
            user_id: Optional user UUID for intra-org isolation.

        Raises:
            NotFoundError: Session not found or already deleted.
        """
        session = await self._repo.soft_delete(org_id, session_id, user_id=user_id)
        if session is None:
            raise NotFoundError(f"Session {session_id} not found")

        logger.info(
            "session.deleted",
            extra={
                "session_id": str(session_id),
                "user_id": str(session.user_id),
            },
        )
