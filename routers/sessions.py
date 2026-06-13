"""Project-scoped session endpoints — /v1/projects/{project_id}/{user_id}/sessions.

Provides five endpoints for managing conversation sessions within a project:
- Create a session (POST)
- List sessions with pagination (GET)
- Get a single session by UUID (GET)
- Get paginated messages for a session (GET)
- Get paginated facts for a session (GET)
- Delete (soft-delete) a session (DELETE)

Every endpoint requires authentication (``require_org_id``) and
project-membership access (``require_project_access``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id, require_project_access
from dependencies.db import get_db
from dependencies.services import get_session_service
from repositories.fact_repository import FactRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
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
    prefix="/v1/projects/{project_id}/{user_id}/sessions",
    tags=["Sessions (Project-scoped)"],
)


# ── Dependency factory for FactService ─────────────────────────────────────


async def get_fact_service_for_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> FactService:
    """FastAPI dependency that yields a FactService scoped to a session."""
    redis_client = getattr(request.app.state, "redis", None)
    return FactService(
        db=db,
        redis_client=redis_client,
        fact_repo=FactRepository(db),
        user_repo=UserRepository(db),
        session_repo=SessionRepository(db),
    )


# ── Create session ─────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=SessionResponse,
    status_code=201,
    summary="Create a session (project-scoped)",
    description="Create a new session for a user within a project.",
)
async def create_session(
    project_id: UUID,
    user_id: UUID,
    body: CreateSessionRequest,
    service: SessionService = Depends(get_session_service),
    org_id: str = Depends(require_project_access),
) -> SessionResponse:
    """Create a new session for a user within a project."""
    return await service.create_session(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=project_id,
        user_id=user_id,
        external_id=body.external_id,
        metadata=body.metadata,
    )


# ── List sessions ──────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=PaginatedResponse[SessionListResponse],
    summary="List sessions (project-scoped)",
    description="List sessions for a user within a project.",
)
async def list_sessions(
    project_id: UUID,
    user_id: UUID,
    service: SessionService = Depends(get_session_service),
    org_id: str = Depends(require_project_access),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of sessions to return (1-200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor from a previous list response.",
    ),
    include_closed: bool = Query(
        default=False,
        description="If true, include closed sessions.",
    ),
) -> PaginatedResponse[SessionListResponse]:
    """List sessions for a user within a project."""
    org_uuid = UUID(org_id) if isinstance(org_id, str) else org_id
    return await service.list_sessions(
        org_id=org_uuid,
        user_id=user_id,
        limit=limit,
        cursor=cursor,
        include_closed=include_closed,
    )


# ── Get session ────────────────────────────────────────────────────────────


@router.get(
    "/{session_id}",
    response_model=SessionResponse,
    summary="Get a session (project-scoped)",
    description="Get a single session by UUID within a project.",
)
async def get_session(
    project_id: UUID,
    user_id: UUID,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    org_id: str = Depends(require_project_access),
) -> SessionResponse:
    """Get session details including aggregate statistics."""
    return await service.get_session(
        org_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        session_id=session_id,
        user_id=user_id,
    )


# ── Get messages ───────────────────────────────────────────────────────────


@router.get(
    "/{session_id}/messages",
    response_model=PaginatedResponse[MessageResponse],
    summary="Get session messages (project-scoped)",
    description="Get paginated messages for a session within a project.",
)
async def get_session_messages(
    project_id: UUID,
    user_id: UUID,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    org_id: str = Depends(require_project_access),
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
        description="Maximum number of messages to return (1-500).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor from a previous messages response.",
    ),
) -> PaginatedResponse[MessageResponse]:
    """Get paginated messages for a session within a project."""
    return await service.get_messages(
        org_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        session_id=session_id,
        limit=limit,
        cursor=cursor,
        user_id=user_id,
    )


# ── Get facts ──────────────────────────────────────────────────────────────


@router.get(
    "/{session_id}/facts",
    response_model=PaginatedResponse[FactResponse],
    summary="Get session facts (project-scoped)",
    description="Get paginated facts for a session within a project.",
)
async def get_session_facts(
    project_id: UUID,
    user_id: UUID,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    fact_service: FactService = Depends(get_fact_service_for_session),
    org_id: str = Depends(require_project_access),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of facts to return (1-200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor from a previous facts response.",
    ),
) -> PaginatedResponse[FactResponse]:
    """Get paginated facts for a session within a project."""
    org_uuid = UUID(org_id) if isinstance(org_id, str) else org_id

    # Verify the session exists before fetching facts.
    try:
        await service.get_session(org_uuid, session_id, user_id=user_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found") from exc

    facts, next_cursor = await fact_service.list_facts_by_session(
        organization_id=org_uuid,
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


# ── Delete session ─────────────────────────────────────────────────────────


@router.delete(
    "/{session_id}",
    status_code=204,
    summary="Delete a session (project-scoped)",
    description="Soft-delete a session within a project.",
)
async def delete_session(
    project_id: UUID,
    user_id: UUID,
    session_id: UUID,
    service: SessionService = Depends(get_session_service),
    org_id: str = Depends(require_project_access),
) -> None:
    """Soft-delete a session within a project."""
    await service.delete_session(
        org_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        session_id=session_id,
        user_id=user_id,
    )
