"""Repository for structured extractions — query access to extraction results.

The ``extract_structured`` worker inserts rows directly via raw SQL. This
repository provides read-only query methods for the structured extraction API.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import SortSpec, resolve_order_by
from models.episode import Episode
from models.structured_extraction import StructuredExtraction

EXTRACTION_SORTABLE_COLUMNS = {
    "sequence_number": Episode.sequence_number,
    "created_at": Episode.created_at,
}
"""Sortable columns for structured extractions (default sequence_number/asc)."""


class StructuredExtractionRepository:
    """Data access for ``structured_extractions`` (read-only for API queries)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_session(
        self,
        org_id: UUID,
        session_id: UUID,
        sort: SortSpec | None = None,
    ) -> list[StructuredExtraction]:
        """Return all extractions for episodes in a session.

        Joins ``structured_extractions`` with ``episodes`` to scope by
        session and org. Default ``sequence_number ASC`` (locked);
        ``created_at`` offered as an alt without breaking the default.

        Args:
            org_id: Tenant scope.
            session_id: The session UUID.
            sort: Validated sort spec.
        """
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("sequence_number", "asc")
        result = await self._db.execute(
            select(StructuredExtraction)
            .join(Episode, Episode.id == StructuredExtraction.episode_id)
            .where(
                Episode.session_id == session_id,
                Episode.organization_id == org_id,
                Episode.is_deleted == False,
            )
            .order_by(
                *resolve_order_by(
                    EXTRACTION_SORTABLE_COLUMNS,
                    Episode.id,
                    req_sort,
                    req_dir,
                    default_sort_by="sequence_number",
                    default_dir="asc",
                )
            )
        )
        return list(result.scalars().all())

    async def get_by_episode(
        self, org_id: UUID, episode_id: UUID
    ) -> StructuredExtraction | None:
        """Return the extraction for a specific episode, if one exists."""
        result = await self._db.execute(
            select(StructuredExtraction)
            .join(Episode, Episode.id == StructuredExtraction.episode_id)
            .where(
                StructuredExtraction.episode_id == episode_id,
                Episode.organization_id == org_id,
                Episode.is_deleted == False,
            )
        )
        return result.scalar_one_or_none()

    async def count_for_session(
        self, org_id: UUID, session_id: UUID
    ) -> int:
        """Count extractions for a session."""
        result = await self._db.execute(
            select(func.count())
            .select_from(StructuredExtraction)
            .join(Episode, Episode.id == StructuredExtraction.episode_id)
            .where(
                Episode.session_id == session_id,
                Episode.organization_id == org_id,
                Episode.is_deleted == False,
            )
        )
        return result.scalar_one()
