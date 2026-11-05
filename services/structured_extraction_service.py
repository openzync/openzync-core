"""Service layer for structured extraction queries — retrieves extraction results.

This service is read-only: structured extractions are produced by the
``extract_structured`` worker and inserted directly into the database.
The service layer handles ownership verification before returning data.
"""

from __future__ import annotations

from uuid import UUID

from core.cursor import decode_page_cursor
from core.exceptions import NotFoundError, ValidationError
from core.sorting import SortSpec
from repositories.session_repository import SessionRepository
from repositories.structured_extraction_repository import (
    EXTRACTION_SORTABLE_COLUMNS,
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
            ValidationError: If the sort key/direction or cursor is invalid.
        """
        # Verify session exists (optionally scoped to project)
        session = await self._session_repo.get_by_uuid(
            org_id=org_id, session_id=session_id, project_id=project_id
        )
        if session is None:
            raise NotFoundError(f"Session '{session_id}' not found")

        # Validate sort + cursor here — the repository takes resolved
        # values only and performs no validation of its own.
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("sequence_number", "asc")
        if req_sort not in EXTRACTION_SORTABLE_COLUMNS:
            raise ValidationError(f"Invalid sort_by: {req_sort!r}")
        if req_dir not in ("asc", "desc"):
            raise ValidationError(f"Invalid sort_dir: {req_dir!r}")
        cursor_value: str | None = None
        cursor_id: UUID | None = None
        if cursor is not None:
            c_sort, c_dir, c_val, c_id = decode_page_cursor(cursor)
            if c_sort != req_sort or c_dir != req_dir:
                raise ValidationError(
                    "Cursor sort mismatch — restart pagination "
                    f"(cursor {c_sort}:{c_dir} vs request {req_sort}:{req_dir})"
                )
            if req_sort == "sequence_number":
                try:
                    int(c_val)
                except ValueError as exc:
                    raise ValidationError(f"Invalid cursor: {exc}") from exc
            elif req_sort == "created_at":
                from datetime import datetime

                try:
                    datetime.fromisoformat(c_val)
                except ValueError as exc:
                    raise ValidationError(f"Invalid cursor: {exc}") from exc
            cursor_value, cursor_id = c_val, c_id

        extractions, next_cursor = await self._repo.get_by_session(
            org_id,
            session_id,
            sort_by=req_sort,
            sort_dir=req_dir,
            limit=limit,
            cursor_value=cursor_value,
            cursor_id=cursor_id,
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
            project_id: Optional project UUID for intra-org isolation —
                forwarded to the repository filter when set.
        """
        return await self._repo.count_for_session(
            org_id, session_id, project_id=project_id
        )
