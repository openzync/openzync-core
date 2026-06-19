"""Service layer for classification queries — retrieves dialog classification results.

This service is read-only: classifications are produced by the ``classify_dialog``
worker and inserted directly into the database.  The service layer handles
ownership verification before returning data.
"""

from __future__ import annotations

from uuid import UUID

from core.exceptions import NotFoundError
from repositories.dialog_classification_repository import (
    DialogClassificationRepository,
)
from repositories.episode_repository import EpisodeRepository
from repositories.session_repository import SessionRepository

from schemas.classifications import ClassificationResponse


class ClassificationService:
    """Business logic for querying dialog classification results."""

    def __init__(
        self,
        repo: DialogClassificationRepository,
        session_repo: SessionRepository,
        episode_repo: EpisodeRepository,
    ) -> None:
        self._repo = repo
        self._session_repo = session_repo
        self._episode_repo = episode_repo

    async def get_classifications_for_session(
        self,
        org_id: UUID,
        session_id: UUID,
    ) -> list[ClassificationResponse]:
        """Return all classifications for episodes in a session.

        Args:
            org_id: The authenticated organization UUID.
            session_id: The session UUID.

        Returns:
            List of ``ClassificationResponse`` objects, ordered by episode
            sequence number.  May be empty if no classifications exist yet.

        Raises:
            NotFoundError: If the session does not exist.
        """
        # Verify session exists
        session = await self._session_repo.get_by_uuid(
            org_id=org_id, session_id=session_id
        )
        if session is None:
            raise NotFoundError(f"Session '{session_id}' not found")

        classifications = await self._repo.get_by_session(org_id, session_id)
        return [
            ClassificationResponse.model_validate(c) for c in classifications
        ]

    async def get_classification_for_episode(
        self,
        org_id: UUID,
        episode_id: UUID,
    ) -> ClassificationResponse | None:
        """Return the classification for a specific episode, or ``None``.

        Args:
            org_id: The authenticated organization UUID.
            episode_id: The episode UUID.

        Returns:
            A ``ClassificationResponse`` or ``None`` if not yet classified.
        """
        classification = await self._repo.get_by_episode(org_id, episode_id)
        if classification is None:
            return None
        return ClassificationResponse.model_validate(classification)

    async def count_classifications_for_session(
        self,
        org_id: UUID,
        session_id: UUID,
    ) -> int:
        """Count how many classified episodes exist in a session."""
        return await self._repo.count_for_session(org_id, session_id)
