"""Service layer for structured extraction queries — retrieves extraction results.

This service is read-only: structured extractions are produced by the
``extract_structured`` worker and inserted directly into the database.
The service layer handles ownership verification before returning data.
"""

from __future__ import annotations

from uuid import UUID

from core.exceptions import NotFoundError
from repositories.session_repository import SessionRepository
from repositories.structured_extraction_repository import (
    StructuredExtractionRepository,
)

from schemas.structured_extractions import (
    StructuredExtractionListResponse,
    StructuredExtractionResponse,
)


class StructuredExtractionService:
    """Business logic for querying structured extraction results."""

    def __init__(
        self,
        repo: StructuredExtractionRepository,
        session_repo: SessionRepository,
    ) -> None:
        self._repo = repo
        self._session_repo = session_repo

    async def get_session_extractions(
        self,
        org_id: UUID,
        session_id: UUID,
        project_id: UUID | None = None,
    ) -> StructuredExtractionListResponse:
        """Return all extractions for episodes in a session.

        Args:
            org_id: The authenticated organization UUID.
            session_id: The session UUID.
            project_id: Optional project UUID for intra-org isolation
                of the session ownership check.

        Returns:
            ``StructuredExtractionListResponse`` with items ordered by
            episode sequence number.  May be empty if no extractions exist.

        Raises:
            NotFoundError: If the session does not exist.
        """
        # Verify session exists (optionally scoped to project)
        session = await self._session_repo.get_by_uuid(
            org_id=org_id, session_id=session_id, project_id=project_id
        )
        if session is None:
            raise NotFoundError(f"Session '{session_id}' not found")

        extractions = await self._repo.get_by_session(org_id, session_id)
        return StructuredExtractionListResponse(
            items=[
                StructuredExtractionResponse.model_validate(e)
                for e in extractions
            ],
            total=len(extractions),
        )

    async def get_episode_extraction(
        self,
        org_id: UUID,
        session_id: UUID,
        episode_id: UUID,
        project_id: UUID | None = None,
    ) -> StructuredExtractionResponse | None:
        """Return the extraction for a specific episode, or ``None``.

        Args:
            org_id: The authenticated organization UUID.
            session_id: The session UUID.
            episode_id: The episode UUID.
            project_id: Optional project UUID for intra-org isolation
                of the session ownership check.

        Returns:
            A ``StructuredExtractionResponse`` or ``None`` if not yet extracted.

        Raises:
            NotFoundError: If the session does not exist.
        """
        session = await self._session_repo.get_by_uuid(
            org_id=org_id, session_id=session_id, project_id=project_id
        )
        if session is None:
            raise NotFoundError(f"Session '{session_id}' not found")

        extraction = await self._repo.get_by_episode(org_id, episode_id)
        if extraction is None:
            return None
        return StructuredExtractionResponse.model_validate(extraction)
