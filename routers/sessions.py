"""Session CRUD endpoints — /v1/projects/{project_id}/sessions.

Provides six endpoints for managing conversation sessions within a project:
- Create a session (POST)
- List sessions with pagination (GET)
- Get a single session by UUID (GET)
- Get paginated messages for a session (GET)
- Get paginated facts for a session (GET)
- Delete (soft-delete) a session (DELETE)

Every endpoint requires project membership (``require_project_membership``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.exceptions import NotFoundError
from dependencies.auth import get_current_user_id
from dependencies.project_auth import require_project_membership
from dependencies.services import get_fact_service, get_session_service
from schemas.common import PaginatedResponse
from schemas.facts import FactResponse
from schemas.sessions import (
    CreateSessionRequest,
    MessageResponse,
    SessionListResponse,
    SessionResponse,
)
from services.fact_service import FactService
from services.session_service import SessionService

router = APIRouter(
    prefix="/v1/projects/{project_id}/sessions",
    tags=["Sessions"],
)


@router.post(
    "",
    response_model=SessionResponse,
    status_code=201,
    summary="Create a session",
    description="Create a new session within a project. The `external_id` "
    "is caller-defined and must be unique per project.",
    responses={
        201: {"description": "Session created successfully."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        409: {
            "description": "A session with this `external_id` already exists "
            "for this project."
        },
    },
)
async def create_session(
    request: Request,
    body: CreateSessionRequest,
    service: SessionService = Depends(get_session_service),
    _: None = Depends(require_project_membership),
    created_by: UUID = Depends(get_current_user_id),
) -> SessionResponse:
    """Create a new session within a project.

    The ``external_id`` is chosen by the caller and must be unique per
    project.  Returns 409 if a session with this ``external_id`` already
    exists.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    return await service.create_session(
        organization_id=org_id,
        project_id=project_id,
        created_by=created_by,
        external_id=body.external_id,
        metadata=body.metadata,
    )


@router.get(
    "",
    response_model=PaginatedResponse[SessionListResponse],
    summary="List sessions",
    description="List sessions for a project with cursor-based pagination. "
    "Excludes the ``__default__`` auto-created session and closed sessions "
    "by default.",
    responses={
        200: {"description": "Paginated list of sessions."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
    },
)
async def list_sessions(
    request: Request,
    service: SessionService = Depends(get_session_service),
    _: None = Depends(require_project_membership),
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
    """List sessions for a project with pagination.

    By default excludes the ``__default__`` auto-created session and
    closed sessions.  Set ``include_closed=true`` to include them.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    return await service.list_sessions(
        org_id=org_id,
        project_id=project_id,
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
        403: {"description": "Not a member of this project."},
        404: {"description": "Session not found."},
    },
)
async def get_session(
    request: Request,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    _: None = Depends(require_project_membership),
) -> SessionResponse:
    """Get session details including aggregate statistics.

    Returns message count, fact count, and session metadata.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    return await service.get_session(
        org_id=org_id,
        session_id=session_id,
        project_id=project_id,
    )


@router.get(
    "/{session_id}/messages",
    response_model=PaginatedResponse[MessageResponse],
    summary="Get session messages",
    description="Get paginated messages for a session, ordered by "
    "``sequence_number`` for deterministic ordering.",
    responses={
        200: {"description": "Paginated list of messages."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        404: {"description": "Session not found."},
    },
)
async def get_session_messages(
    request: Request,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    _: None = Depends(require_project_membership),
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
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    return await service.get_messages(
        org_id=org_id,
        session_id=session_id,
        limit=limit,
        cursor=cursor,
        project_id=project_id,
    )


@router.get(
    "/{session_id}/facts",
    response_model=PaginatedResponse[FactResponse],
    summary="Get session facts",
    description="Get paginated facts extracted from messages in a session. "
    "Ordered by creation time (newest first).",
    responses={
        200: {"description": "Paginated list of facts."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        404: {"description": "Session not found."},
    },
)
async def get_session_facts(
    request: Request,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    fact_service: FactService = Depends(get_fact_service),
    _: None = Depends(require_project_membership),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of facts to return (1–200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor from a previous facts response.",
    ),
) -> PaginatedResponse[FactResponse]:
    """Get paginated facts for a session.

    Returns facts extracted from messages in this session, ordered by
    creation time (newest first).  Only non-invalidated facts are included.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    # Verify the session exists before fetching facts.
    # NotFoundError propagates to global exception handler → 404.
    await service.get_session(org_id, session_id, project_id=project_id)

    facts, next_cursor = await fact_service.list_facts_by_session(
        organization_id=org_id,
        session_id=session_id,
        limit=limit,
        cursor=cursor,
    )

    items = [FactResponse.model_validate(f) for f in facts]

    return PaginatedResponse[FactResponse](
        data=items,
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
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
        403: {"description": "Not a member of this project."},
        404: {"description": "Session not found."},
    },
)
async def delete_session(
    request: Request,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    _: None = Depends(require_project_membership),
) -> None:
    """Delete (soft-delete) a session.

    Sets ``is_deleted = True`` and unlinks episodes from the session.
    Episodes are preserved as orphaned history for audit purposes.
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    await service.delete_session(
        org_id=org_id,
        session_id=session_id,
        project_id=project_id,
    )
