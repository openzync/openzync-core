"""Repository for structured extractions — query access to extraction results.

The ``extract_structured`` worker inserts rows directly via raw SQL. This
repository provides read-only query methods for the structured extraction API.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cursor import decode_page_cursor, encode_cursor
from core.sorting import SortSpec, resolve_order_by
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
        sort: SortSpec | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[StructuredExtraction], str | None]:
        """Return a page of extractions for episodes in a session.

        Joins ``structured_extractions`` with ``episodes`` to scope by
        session and org. Default ``sequence_number ASC`` (locked);
        ``created_at`` offered as an alt without breaking the default.
        Cursor encodes ``sort_by:sort_dir`` and fails closed (422) on
        mismatch — same keyset pattern as ``SessionRepository.list``.

        Args:
            org_id: Tenant scope.
            session_id: The session UUID.
            sort: Validated sort spec.
            limit: Maximum results per page (capped at 500).
            cursor: Opaque base64 cursor from a previous page.

        Returns:
            Tuple of ``(extractions, next_cursor)`` — ``next_cursor``
            is ``None`` when there are no more pages.
        """
        from core.exceptions import ValidationError

        effective_limit = min(limit, 500) + 1  # +1 to detect has_more
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("sequence_number", "asc")
        if req_sort not in EXTRACTION_SORTABLE_COLUMNS:
            raise ValidationError(f"Invalid sort_by: {req_sort!r}")
        if req_dir not in ("asc", "desc"):
            raise ValidationError(f"Invalid sort_dir: {req_dir!r}")

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

        col = EXTRACTION_SORTABLE_COLUMNS[req_sort]
        if cursor is not None:
            c_sort, c_dir, c_val, c_ep_id = decode_page_cursor(cursor)
            if c_sort != req_sort or c_dir != req_dir:
                raise ValidationError(
                    "Cursor sort mismatch — restart pagination "
                    f"(cursor {c_sort}:{c_dir} vs request {req_sort}:{req_dir})"
                )
            if req_sort == "sequence_number":
                try:
                    c_num = int(c_val)
                except ValueError as e:
                    raise ValidationError(f"Invalid cursor: {e}") from e
                cmp = col > c_num if req_dir == "asc" else col < c_num
                query = query.where(
                    or_(cmp, and_(col == c_num, Episode.id > c_ep_id))
                )
            else:
                try:
                    c_at = datetime.fromisoformat(c_val)
                except ValueError as e:
                    raise ValidationError(f"Invalid cursor: {e}") from e
                cmp = col > c_at if req_dir == "asc" else col < c_at
                query = query.where(
                    or_(cmp, and_(col == c_at, Episode.id > c_ep_id))
                )

        query = query.order_by(
            *resolve_order_by(
                EXTRACTION_SORTABLE_COLUMNS,
                Episode.id,
                req_sort,
                req_dir,
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
            if req_sort == "sequence_number":
                val = str(last_ep.sequence_number)
            else:
                val = last_ep.created_at.isoformat()
            next_cursor = encode_cursor(f"{req_sort}:{req_dir}:{val}|{last_ep.id.hex}")

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
        self, org_id: UUID, session_id: UUID
    ) -> int:
        """Count extractions for a session.

        Archived projects are excluded: the ``extract_structured`` worker
        early-returns on them, so their rows are terminal, not a running
        total. The sibling message/fact counts do not filter — they are
        inventory.
        """
        result = await self._db.execute(
            select(func.count())
            .select_from(StructuredExtraction)
            .join(Episode, Episode.id == StructuredExtraction.episode_id)
            .where(
                Episode.session_id == session_id,
                Episode.organization_id == org_id,
                Episode.is_deleted == False,
                StructuredExtraction.project_id.not_in(archived_project_ids()),
            )
        )
        return result.scalar_one()
