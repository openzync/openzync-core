"""Repository for structured extractions — query access to extraction results.

The ``extract_structured`` worker inserts rows directly via raw SQL. This
repository provides read-only query methods for the structured extraction API.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.episode import Episode
from models.structured_extraction import StructuredExtraction


class StructuredExtractionRepository:
    """Data access for ``structured_extractions`` (read-only for API queries)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_session(
        self, org_id: UUID, session_id: UUID
    ) -> list[StructuredExtraction]:
        """Return all extractions for episodes in a session.

        Joins ``structured_extractions`` with ``episodes`` to scope by
        session and org.  Results are ordered by episode sequence number.
        """
        result = await self._db.execute(
            select(StructuredExtraction)
            .join(Episode, Episode.id == StructuredExtraction.episode_id)
            .where(
                Episode.session_id == session_id,
                Episode.organization_id == org_id,
                Episode.is_deleted == False,
            )
            .order_by(Episode.sequence_number)
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
