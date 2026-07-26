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
        project_id: UUID | None = None,
    ) -> list[ClassificationResponse]:
        """Return all classifications for episodes in a session.

        Args:
            org_id: The authenticated organization UUID.
            session_id: The session UUID.
            project_id: Optional project UUID for intra-org isolation
                of the session ownership check.

        Returns:
            List of ``ClassificationResponse`` objects, ordered by episode
            sequence number.  May be empty if no classifications exist yet.

        Raises:
            NotFoundError: If the session does not exist.
        """
        # Verify session exists (optionally scoped to project)
        session = await self._session_repo.get_by_uuid(
            org_id=org_id, session_id=session_id, project_id=project_id
        )
        if session is None:
            raise NotFoundError(f"Session '{session_id}' not found")

        classifications = await self._repo.get_by_session(org_id, session_id)
        if not classifications:
            return []

        # Batch-fetch episode content to avoid N+1
        episode_ids = [c.episode_id for c in classifications]
        episode_map = await self._episode_repo.get_content_batch(episode_ids, org_id=org_id)

        return [
            ClassificationResponse(
                id=c.id,
                episode_id=c.episode_id,
                intent=c.intent,
                emotion=c.emotion,
                valence=c.valence,
                arousal=c.arousal,
                confidence=c.confidence,
                created_at=c.created_at,
                message=(entry := episode_map.get(c.episode_id, ("", "")))[0],
                role=entry[1],
            )
            for c in classifications
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
            A ``ClassificationResponse`` with ``message`` and ``role``
            populated, or ``None`` if not yet classified.
        """
        classification = await self._repo.get_by_episode(org_id, episode_id)
        if classification is None:
            return None

        episode_map = await self._episode_repo.get_content_batch([episode_id], org_id=org_id)
        content, role = episode_map.get(episode_id, ("", ""))

        return ClassificationResponse(
            id=classification.id,
            episode_id=classification.episode_id,
            intent=classification.intent,
            emotion=classification.emotion,
            valence=classification.valence,
            arousal=classification.arousal,
            confidence=classification.confidence,
            created_at=classification.created_at,
            message=content,
            role=role,
        )

    async def count_classifications_for_session(
        self,
        org_id: UUID,
        session_id: UUID,
        project_id: UUID | None = None,
    ) -> int:
        """Count how many classified episodes exist in a session.

        Args:
            org_id: The authenticated organization UUID.
            session_id: The session UUID.
            project_id: Optional project UUID (reserved for future
                defense-in-depth — not yet used by the repo layer).
        """
        return await self._repo.count_for_session(org_id, session_id)
