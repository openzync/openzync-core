"""Repository for dialog classifications — query access to classification results.

The ``classify_dialog`` worker inserts rows directly via raw SQL.  This
repository provides read-only query methods for the classification API.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import SortSpec, resolve_order_by
from models.dialog_classification import DialogClassification
from models.episode import Episode

CLASSIFICATION_SORTABLE_COLUMNS = {
    "sequence_number": Episode.sequence_number,
    "created_at": Episode.created_at,
}
"""Sortable columns for classifications (default sequence_number/asc)."""


class DialogClassificationRepository:
    """Data access for ``dialog_classifications`` (read-only for API queries)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_session(
        self,
        org_id: UUID,
        session_id: UUID,
        sort: SortSpec | None = None,
    ) -> list[DialogClassification]:
        """Return all classifications for episodes in a session.

        Joins ``dialog_classifications`` with ``episodes`` to scope by
        session. Default ``sequence_number ASC`` (locked); ``created_at``
        offered as an alt without breaking the default.

        Args:
            org_id: Tenant scope.
            session_id: The session UUID.
            sort: Validated sort spec.
        """
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("sequence_number", "asc")
        result = await self._db.execute(
            select(DialogClassification)
            .join(Episode, Episode.id == DialogClassification.episode_id)
            .where(
                Episode.session_id == session_id,
                DialogClassification.organization_id == org_id,
                Episode.is_deleted == False,
            )
            .order_by(
                *resolve_order_by(
                    CLASSIFICATION_SORTABLE_COLUMNS,
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
