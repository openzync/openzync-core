"""Repository for structured extractions — query access to extraction results.

The ``extract_structured`` worker inserts rows directly via raw SQL. This
repository provides read-only query methods for the structured extraction API.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import resolve_order_by
from models.episode import Episode
from models.structured_extraction import StructuredExtraction
from repositories.project_repository import archived_project_ids

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
        *,
        sort_by: str,
        sort_dir: str,
        limit: int = 100,
        cursor_value: str | None = None,
        cursor_id: UUID | None = None,
    ) -> tuple[list[StructuredExtraction], str | None]:
        """Return a page of extractions for episodes in a session.

        Takes resolved sort/cursor values — the service layer validates
        the ``SortSpec`` and decodes the opaque cursor before calling.
        This repository performs no validation and raises no
        ``ValidationError``.

        # ⚠️ BREAKING: signature changed from
        # ``(org_id, session_id, sort=None, limit, cursor=None)`` to
        # resolved ``(sort_by, sort_dir, limit, cursor_value, cursor_id)``.

        Args:
            org_id: Tenant scope.
            session_id: The session UUID.
            sort_by: Validated sort key (``sequence_number``/``created_at``).
            sort_dir: Validated direction (``asc``/``desc``).
            limit: Maximum results per page (capped at 500).
            cursor_value: Resolved keyset position, or ``None`` for page one.
            cursor_id: Resolved tiebreak row id, or ``None`` for page one.

        Returns:
            Tuple of ``(extractions, next_cursor)`` — ``next_cursor``
            is ``None`` when there are no more pages.
        """
        from core.cursor import encode_cursor

        effective_limit = min(limit, 500) + 1  # +1 to detect has_more

        query = (
            select(StructuredExtraction, Episode)
            .join(Episode, Episode.id == StructuredExtraction.episode_id)
            .where(
                Episode.session_id == session_id,
                Episode.organization_id == org_id,
                Episode.is_deleted == False,
                StructuredExtraction.project_id.not_in(archived_project_ids()),
            )
        )

        col = EXTRACTION_SORTABLE_COLUMNS[sort_by]
        if cursor_value is not None and cursor_id is not None:
            if sort_by == "sequence_number":
                c_num = int(cursor_value)
                cmp = col > c_num if sort_dir == "asc" else col < c_num
                query = query.where(
                    or_(cmp, and_(col == c_num, Episode.id > cursor_id))
                )
            else:
                c_at = datetime.fromisoformat(cursor_value)
                cmp = col > c_at if sort_dir == "asc" else col < c_at
                query = query.where(
                    or_(cmp, and_(col == c_at, Episode.id > cursor_id))
                )

        query = query.order_by(
            *resolve_order_by(
                EXTRACTION_SORTABLE_COLUMNS,
                Episode.id,
                sort_by,
                sort_dir,
                default_sort_by="sequence_number",
                default_dir="asc",
            )
        ).limit(effective_limit)

        result = await self._db.execute(query)
        pairs = list(result.all())

        has_more = len(pairs) == effective_limit
        page = pairs[: min(limit, 500)] if has_more else pairs

        next_cursor: str | None = None
        if has_more and page:
            _, last_ep = page[-1]
            if sort_by == "sequence_number":
                val = str(last_ep.sequence_number)
            else:
                val = last_ep.created_at.isoformat()
            next_cursor = encode_cursor(f"{sort_by}:{sort_dir}:{val}|{last_ep.id.hex}")

        return [e for e, _ in page], next_cursor

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
        self,
        org_id: UUID,
        session_id: UUID,
        project_id: UUID | None = None,
    ) -> int:
        """Count extractions for a session, optionally scoped to a project.

        # ⚠️ BREAKING: added optional ``project_id`` — when set, only
        # extractions whose episode belongs to that project are counted.

        Archived projects are excluded: the ``extract_structured`` worker
        early-returns on them, so their rows are terminal, not a running
        total. The sibling message/fact counts do not filter — they are
        inventory.
        """
        conditions = [
            Episode.session_id == session_id,
            Episode.organization_id == org_id,
            Episode.is_deleted == False,
            StructuredExtraction.project_id.not_in(archived_project_ids()),
        ]
        if project_id is not None:
            conditions.append(Episode.project_id == project_id)
        result = await self._db.execute(
            select(func.count())
            .select_from(StructuredExtraction)
            .join(Episode, Episode.id == StructuredExtraction.episode_id)
            .where(*conditions)
        )
        return result.scalar_one()
