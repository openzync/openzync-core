"""Structured extraction query endpoints — retrieve extraction results.

Endpoints:
    GET /v1/projects/{project_id}/sessions/{session_id}/structured-extractions
        — List extractions for all episodes in a session.
    GET /v1/projects/{project_id}/sessions/{session_id}/structured-extractions/{episode_id}
        — Get extraction for a specific episode.

Every endpoint is guarded by ``require_project_membership`` for unified
authentication and project authorization.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from dependencies.project_auth import require_project_membership
from repositories.session_repository import SessionRepository
from repositories.structured_extraction_repository import (
    StructuredExtractionRepository,
)
from schemas.structured_extractions import (
    StructuredExtractionListResponse,
    StructuredExtractionResponse,
)
from services.structured_extraction_service import (
    StructuredExtractionService,
)

router = APIRouter(
    prefix="/v1/projects/{project_id}/sessions/{session_id}/structured-extractions",
    tags=["Structured Extraction"],
)


def _get_extraction_service(
    db: AsyncSession = Depends(get_db),
) -> StructuredExtractionService:
    """Dependency factory for ``StructuredExtractionService``."""
    return StructuredExtractionService(
        repo=StructuredExtractionRepository(db),
        session_repo=SessionRepository(db),
    )


@router.get(
    "",
    response_model=StructuredExtractionListResponse,
    dependencies=[Depends(require_project_membership)],
)
async def list_structured_extractions(
    request: Request,
    session_id: UUID = Path(...),
    service: StructuredExtractionService = Depends(_get_extraction_service),
) -> StructuredExtractionListResponse:
    """List all structured extractions for episodes in a session.

    Returns an empty list if no episodes in the session have been processed
    by the ``extract_structured`` worker yet, or if no structured schemas
    are configured for the organization.
    """
    org_id = UUID(request.state.org_id)
    return await service.get_session_extractions(
        org_id=org_id,
        session_id=session_id,
    )


@router.get(
    "/{episode_id}",
    response_model=StructuredExtractionResponse,
    dependencies=[Depends(require_project_membership)],
)
async def get_episode_extraction(
    request: Request,
    session_id: UUID = Path(...),
    episode_id: UUID = Path(...),
    service: StructuredExtractionService = Depends(_get_extraction_service),
) -> StructuredExtractionResponse:
    """Get the structured extraction for a specific episode in a session.

    Returns 404 if the episode has not been processed yet or no matching
    extraction exists.
    """
    org_id = UUID(request.state.org_id)
    result = await service.get_episode_extraction(
        org_id=org_id,
        session_id=session_id,
        episode_id=episode_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No structured extraction found for episode "
                f"'{episode_id}' in session '{session_id}'"
            ),
        )
    return result
