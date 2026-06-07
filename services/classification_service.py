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
from repositories.user_repository import UserRepository
from schemas.classifications import ClassificationResponse


class ClassificationService:
    """Business logic for querying dialog classification results."""

    def __init__(
        self,
        repo: DialogClassificationRepository,
        user_repo: UserRepository,
        session_repo: SessionRepository,
        episode_repo: EpisodeRepository,
    ) -> None:
        self._repo = repo
        self._user_repo = user_repo
        self._session_repo = session_repo
        self._episode_repo = episode_repo

    async def get_classifications_for_session(
        self,
        org_id: UUID,
        user_id: UUID,
        session_id: UUID,
    ) -> list[ClassificationResponse]:
        """Return all classifications for episodes in a session.

        Args:
            org_id: The authenticated organization UUID.
            user_id: The user UUID (must belong to the org).
            session_id: The session UUID (must belong to the user).

        Returns:
            List of ``ClassificationResponse`` objects, ordered by episode
            sequence number.  May be empty if no classifications exist yet.

        Raises:
            NotFoundError: If the user or session does not exist or does
                not belong to the org.
        """
        # Verify user belongs to org
        user = await self._user_repo.get_by_uuid(org_id, user_id)
        if user is None:
            raise NotFoundError(f"User '{user_id}' not found in organization")

        # Verify session belongs to user
        session = await self._session_repo.get_by_uuid(
            org_id=org_id, session_id=session_id, user_id=user_id
        )
        if session is None:
            raise NotFoundError(
                f"Session '{session_id}' not found for user '{user_id}'"
            )

        classifications = await self._repo.get_by_session(org_id, session_id)
        return [
            ClassificationResponse.model_validate(c) for c in classifications
        ]

    async def get_classification_for_episode(
        self,
        org_id: UUID,
        user_id: UUID,
        episode_id: UUID,
    ) -> ClassificationResponse | None:
        """Return the classification for a specific episode, or ``None``.

        Args:
            org_id: The authenticated organization UUID.
            user_id: The user UUID (must belong to the org).
            episode_id: The episode UUID.

        Returns:
            A ``ClassificationResponse`` or ``None`` if not yet classified.

        Raises:
            NotFoundError: If the user does not belong to the org.
        """
        user = await self._user_repo.get_by_uuid(org_id, user_id)
        if user is None:
            raise NotFoundError(f"User '{user_id}' not found in organization")

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
