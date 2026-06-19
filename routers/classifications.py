"""Classification query endpoints — retrieve dialog classification results.

Endpoints:
    GET /v1/projects/{project_id}/sessions/{session_id}/classifications
        — List classifications for all episodes in a session.
    GET /v1/projects/{project_id}/sessions/{session_id}/classifications/{episode_id}
        — Get classification for a specific episode.

Every endpoint is guarded by ``require_project_membership`` for unified
authentication and project authorization.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from dependencies.project_auth import require_project_membership
from repositories.dialog_classification_repository import (
    DialogClassificationRepository,
)
from repositories.episode_repository import EpisodeRepository
from repositories.session_repository import SessionRepository
from schemas.classifications import (
    ClassificationListResponse,
    ClassificationResponse,
)
from services.classification_service import ClassificationService

router = APIRouter(
    prefix="/v1/projects/{project_id}/sessions/{session_id}/classifications",
    tags=["Classification"],
)


def _get_classification_service(
    db: AsyncSession = Depends(get_db),
) -> ClassificationService:
    """Dependency factory for ``ClassificationService``."""
    return ClassificationService(
        repo=DialogClassificationRepository(db),
        session_repo=SessionRepository(db),
        episode_repo=EpisodeRepository(db),
    )


@router.get(
    "",
    response_model=ClassificationListResponse,
    dependencies=[Depends(require_project_membership)],
)
async def list_classifications(
    request: Request,
    session_id: UUID = Path(...),
    service: ClassificationService = Depends(_get_classification_service),
) -> ClassificationListResponse:
    """List all classifications for episodes in a session.

    Returns an empty list if no episodes in the session have been classified
    yet (the ``classify_dialog`` worker may not have run yet).
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])
    classifications = await service.get_classifications_for_session(
        org_id=org_id,
        session_id=session_id,
        project_id=project_id,
    )
    return ClassificationListResponse(
        data=classifications,
        total=len(classifications),
    )


@router.get(
    "/{episode_id}",
    response_model=ClassificationResponse,
    dependencies=[Depends(require_project_membership)],
)
async def get_episode_classification(
    request: Request,
    session_id: UUID = Path(...),
    episode_id: UUID = Path(...),
    service: ClassificationService = Depends(_get_classification_service),
) -> ClassificationResponse:
    """Get the classification for a specific episode in a session.

    Returns 404 if the episode has not been classified yet.
    """
    org_id = UUID(request.state.org_id)
    result = await service.get_classification_for_episode(
        org_id=org_id,
        episode_id=episode_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Episode '{episode_id}' has not been classified yet",
        )
    return result
