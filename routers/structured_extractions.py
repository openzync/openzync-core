"""Structured extraction query endpoints — retrieve extraction results.

Endpoints:
    GET /v1/users/{user_id}/sessions/{session_id}/structured-extractions
        — List extractions for all episodes in a session.
    GET /v1/users/{user_id}/sessions/{session_id}/structured-extractions/{episode_id}
        — Get extraction for a specific episode.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id
from dependencies.db import get_db
from repositories.session_repository import SessionRepository
from repositories.structured_extraction_repository import (
    StructuredExtractionRepository,
)
from repositories.user_repository import UserRepository
from schemas.structured_extractions import (
    StructuredExtractionListResponse,
    StructuredExtractionResponse,
)
from services.structured_extraction_service import (
    StructuredExtractionService,
)

router = APIRouter(
    prefix="/v1/users/{user_id}/sessions/{session_id}/structured-extractions",
    tags=["Structured Extraction"],
)


def _get_extraction_service(
    db: AsyncSession = Depends(get_db),
) -> StructuredExtractionService:
    """Dependency factory for ``StructuredExtractionService``."""
    return StructuredExtractionService(
        repo=StructuredExtractionRepository(db),
        user_repo=UserRepository(db),
        session_repo=SessionRepository(db),
    )


@router.get(
    "",
    response_model=StructuredExtractionListResponse,
)
async def list_structured_extractions(
    user_id: UUID,
    session_id: UUID,
    service: StructuredExtractionService = Depends(_get_extraction_service),
    org_id: str = Depends(require_org_id),
) -> StructuredExtractionListResponse:
    """List all structured extractions for episodes in a session.

    Returns an empty list if no episodes in the session have been processed
    by the ``extract_structured`` worker yet, or if no structured schemas
    are configured for the organization.
    """
    return await service.get_session_extractions(
        org_id=UUID(org_id),
        user_id=user_id,
        session_id=session_id,
    )


@router.get(
    "/{episode_id}",
    response_model=StructuredExtractionResponse,
)
async def get_episode_extraction(
    user_id: UUID,
    session_id: UUID,
    episode_id: UUID,
    service: StructuredExtractionService = Depends(_get_extraction_service),
    org_id: str = Depends(require_org_id),
) -> StructuredExtractionResponse:
    """Get the structured extraction for a specific episode in a session.

    Returns 404 if the episode has not been processed yet or no matching
    extraction exists.
    """
    result = await service.get_episode_extraction(
        org_id=UUID(org_id),
        user_id=user_id,
        session_id=session_id,
        episode_id=episode_id,
    )
    if result is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No structured extraction found for episode "
                f"'{episode_id}' in session '{session_id}'"
            ),
        )
    return result
