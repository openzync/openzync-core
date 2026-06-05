"""Session CRUD endpoints — /v1/users/{user_id}/sessions.

Provides five endpoints for managing conversation sessions:
- Create a session (POST)
- List sessions with pagination (GET)
- Get a single session by UUID (GET)
- Get paginated messages for a session (GET)
- Delete (soft-delete) a session (DELETE)

Every endpoint requires authentication (``require_org_id``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from dependencies.auth import require_org_id
from dependencies.services import get_session_service
from schemas.common import PaginatedResponse
from schemas.sessions import (
    CreateSessionRequest,
    MessageResponse,
    SessionListResponse,
    SessionResponse,
)
from services.session_service import SessionService

router = APIRouter(
    prefix="/v1/users/{user_id}/sessions",
    tags=["Sessions"],
)


@router.post(
    "",
    response_model=SessionResponse,
    status_code=201,
    summary="Create a session",
    description="Create a new session for the given user.  The `external_id` "
    "is caller-defined and must be unique per user.",
    responses={
        201: {"description": "Session created successfully."},
        401: {"description": "Missing or invalid authentication."},
        409: {
            "description": "A session with this `external_id` already exists "
            "for this user."
        },
    },
)
async def create_session(
    user_id: UUID,
    body: CreateSessionRequest,
    service: SessionService = Depends(get_session_service),
    org_id: str = Depends(require_org_id),
) -> SessionResponse:
    """Create a new session for a user.

    The ``external_id`` is chosen by the caller and must be unique per
    user.  Returns 409 if a session with this ``external_id`` already
    exists.
    """
    return await service.create_session(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        user_id=user_id,
        external_id=body.external_id,
        metadata=body.metadata,
    )


@router.get(
    "",
    response_model=PaginatedResponse[SessionListResponse],
    summary="List sessions",
    description="List sessions for a user with cursor-based pagination. "
    "Excludes the ``__default__`` auto-created session and closed sessions "
    "by default.",
    responses={
        200: {"description": "Paginated list of sessions."},
        401: {"description": "Missing or invalid authentication."},
    },
)
async def list_sessions(
    user_id: UUID,
    service: SessionService = Depends(get_session_service),
    _org_id: str = Depends(require_org_id),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of sessions to return (1–200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor from a previous list response for "
        "pagination.",
    ),
    include_closed: bool = Query(
        default=False,
        description="If true, include closed sessions in the results.",
    ),
) -> PaginatedResponse[SessionListResponse]:
    """List sessions for a user with pagination.

    By default excludes the ``__default__`` auto-created session and
    closed sessions.  Set ``include_closed=true`` to include them.
    """
    return await service.list_sessions(
        user_id=user_id,
        limit=limit,
        cursor=cursor,
        include_closed=include_closed,
    )


@router.get(
    "/{session_id}",
    response_model=SessionResponse,
    summary="Get a session",
    description="Get a single session by its UUID, including aggregate "
    "statistics (message count, fact count).",
    responses={
        200: {"description": "Session details."},
        401: {"description": "Missing or invalid authentication."},
        404: {"description": "Session not found."},
    },
)
async def get_session(
    user_id: UUID,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    _org_id: str = Depends(require_org_id),
) -> SessionResponse:
    """Get session details including aggregate statistics.

    Returns message count, fact count, and session metadata.
    """
    return await service.get_session(session_id=session_id)


@router.get(
    "/{session_id}/messages",
    response_model=PaginatedResponse[MessageResponse],
    summary="Get session messages",
    description="Get paginated messages for a session, ordered by "
    "``sequence_number`` for deterministic ordering.",
    responses={
        200: {"description": "Paginated list of messages."},
        401: {"description": "Missing or invalid authentication."},
        404: {"description": "Session not found."},
    },
)
async def get_session_messages(
    user_id: UUID,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    _org_id: str = Depends(require_org_id),
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
        description="Maximum number of messages to return (1–500).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor from a previous messages response for "
        "pagination.",
    ),
) -> PaginatedResponse[MessageResponse]:
    """Get paginated messages for a session.

    Messages are ordered by ``sequence_number`` for deterministic
    ordering (not by ``created_at``, which can have ties).
    """
    return await service.get_messages(
        session_id=session_id,
        limit=limit,
        cursor=cursor,
    )


@router.delete(
    "/{session_id}",
    status_code=204,
    summary="Delete a session",
    description="Soft-delete a session.  Episodes are unlinked from the "
    "session but preserved as orphaned history for audit purposes.",
    responses={
        204: {"description": "Session deleted successfully (no content)."},
        401: {"description": "Missing or invalid authentication."},
        404: {"description": "Session not found."},
    },
)
async def delete_session(
    user_id: UUID,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    _org_id: str = Depends(require_org_id),
) -> None:
    """Delete (soft-delete) a session.

    Sets ``is_deleted = True`` and unlinks episodes from the session.
    Episodes are preserved as orphaned history for audit purposes.
    """
    await service.delete_session(session_id=session_id)
