"""Classification query endpoints — retrieve dialog classification results.

Endpoints:
    GET /v1/users/{user_id}/sessions/{session_id}/classifications
        — List classifications for all episodes in a session.
    GET /v1/users/{user_id}/sessions/{session_id}/classifications/{episode_id}
        — Get classification for a specific episode.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id
from dependencies.db import get_db
from repositories.dialog_classification_repository import (
    DialogClassificationRepository,
)
from repositories.episode_repository import EpisodeRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from schemas.classifications import (
    ClassificationListResponse,
    ClassificationResponse,
)
from services.classification_service import ClassificationService

router = APIRouter(
    prefix="/v1/users/{user_id}/sessions/{session_id}/classifications",
    tags=["Classification"],
)


def _get_classification_service(
    db: AsyncSession = Depends(get_db),
) -> ClassificationService:
    """Dependency factory for ``ClassificationService``."""
    return ClassificationService(
        repo=DialogClassificationRepository(db),
        user_repo=UserRepository(db),
        session_repo=SessionRepository(db),
        episode_repo=EpisodeRepository(db),
    )


@router.get(
    "",
    response_model=ClassificationListResponse,
)
async def list_classifications(
    user_id: UUID,
    session_id: UUID,
    service: ClassificationService = Depends(_get_classification_service),
    org_id: str = Depends(require_org_id),
) -> ClassificationListResponse:
    """List all classifications for episodes in a session.

    Returns an empty list if no episodes in the session have been classified
    yet (the ``classify_dialog`` worker may not have run yet).
    """
    classifications = await service.get_classifications_for_session(
        org_id=UUID(org_id),
        user_id=user_id,
        session_id=session_id,
    )
    return ClassificationListResponse(
        data=classifications,
        total=len(classifications),
    )


@router.get(
    "/{episode_id}",
    response_model=ClassificationResponse,
)
async def get_episode_classification(
    user_id: UUID,
    session_id: UUID,
    episode_id: UUID,
    service: ClassificationService = Depends(_get_classification_service),
    org_id: str = Depends(require_org_id),
) -> ClassificationResponse:
    """Get the classification for a specific episode in a session.

    Returns 404 if the episode has not been classified yet.
    """
    result = await service.get_classification_for_episode(
        org_id=UUID(org_id),
        user_id=user_id,
        episode_id=episode_id,
    )
    if result is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Episode '{episode_id}' has not been classified yet",
        )
    return result
