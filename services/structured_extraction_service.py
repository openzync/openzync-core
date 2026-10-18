"""Service layer for structured extraction queries — retrieves extraction results.

This service is read-only: structured extractions are produced by the
``extract_structured`` worker and inserted directly into the database.
The service layer handles ownership verification before returning data.
"""

from __future__ import annotations

from uuid import UUID

from core.exceptions import NotFoundError
from core.sorting import SortSpec
from repositories.session_repository import SessionRepository
from repositories.structured_extraction_repository import (
    StructuredExtractionRepository,
)
from schemas.structured_extractions import (
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
        sort: SortSpec | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[StructuredExtractionResponse], str | None]:
        """Return a page of extractions for episodes in a session.

        Default ``sequence_number ASC`` (locked); ``created_at`` alt
        offered without breaking the default.

        Args:
            org_id: The authenticated organization UUID.
            session_id: The session UUID.
            project_id: Optional project UUID for intra-org isolation
                of the session ownership check.
            sort: Validated sort spec (forwarded opaque to the repository).
            limit: Maximum results per page (capped at 500).
            cursor: Opaque cursor from a previous page.

        Returns:
            Tuple of ``(extractions, next_cursor)`` — ``next_cursor``
            is ``None`` when there are no more pages. May be empty if no
            extractions exist.

        Raises:
            NotFoundError: If the session does not exist.
        """
        # Verify session exists (optionally scoped to project)
        session = await self._session_repo.get_by_uuid(
            org_id=org_id, session_id=session_id, project_id=project_id
        )
        if session is None:
            raise NotFoundError(f"Session '{session_id}' not found")

        extractions, next_cursor = await self._repo.get_by_session(
            org_id, session_id, sort=sort, limit=limit, cursor=cursor
        )
        return (
            [
                StructuredExtractionResponse.model_validate(e)
                for e in extractions
            ],
            next_cursor,
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

    async def count_extractions_for_session(
        self,
        org_id: UUID,
        session_id: UUID,
        project_id: UUID | None = None,
    ) -> int:
        """Count how many extracted episodes exist in a session.

        Args:
            org_id: The authenticated organization UUID.
            session_id: The session UUID.
            project_id: Optional project UUID (reserved for future
                defense-in-depth — not yet used by the repo layer).
        """
        return await self._repo.count_for_session(org_id, session_id)
