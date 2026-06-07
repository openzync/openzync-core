"""Repository for dialog classifications — query access to classification results.

The ``classify_dialog`` worker inserts rows directly via raw SQL.  This
repository provides read-only query methods for the classification API.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.dialog_classification import DialogClassification
from models.episode import Episode


class DialogClassificationRepository:
    """Data access for ``dialog_classifications`` (read-only for API queries)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_session(
        self, org_id: UUID, session_id: UUID
    ) -> list[DialogClassification]:
        """Return all classifications for episodes in a session.

        Joins ``dialog_classifications`` with ``episodes`` to scope by
        session.  Results are ordered by episode sequence number.
        """
        result = await self._db.execute(
            select(DialogClassification)
            .join(Episode, Episode.id == DialogClassification.episode_id)
            .where(
                Episode.session_id == session_id,
                DialogClassification.organization_id == org_id,
                Episode.is_deleted == False,
            )
            .order_by(Episode.sequence_number)
        )
        return list(result.scalars().all())

    async def get_by_episode(
        self, org_id: UUID, episode_id: UUID
    ) -> DialogClassification | None:
        """Return the classification for a specific episode, if one exists."""
        result = await self._db.execute(
            select(DialogClassification).where(
                DialogClassification.episode_id == episode_id,
                DialogClassification.organization_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def count_for_session(
        self, org_id: UUID, session_id: UUID
    ) -> int:
        """Count classifications for a session."""
        result = await self._db.execute(
            select(func.count())
            .select_from(DialogClassification)
            .join(Episode, Episode.id == DialogClassification.episode_id)
            .where(
                Episode.session_id == session_id,
                DialogClassification.organization_id == org_id,
                Episode.is_deleted == False,
            )
        )
        return result.scalar_one()
