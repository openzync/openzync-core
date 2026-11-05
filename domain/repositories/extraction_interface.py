"""Extraction repository interface — session-scoped extraction reads."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class ExtractionRepository(Protocol):
    """Persistence contract for structured-extraction reads.

    The service layer validates sort/cursor input and forwards resolved
    values — this interface takes no opaque sort or cursor strings.
    """

    async def get_by_session(
        self,
        org_id: UUID,
        session_id: UUID,
        *,
        sort_by: str,
        sort_dir: str,
        limit: int,
        cursor_value: str | None,
        cursor_id: UUID | None,
    ) -> tuple[list[object], str | None]:
        """Return one page of extractions plus the next cursor (or ``None``)."""
        ...

    async def count_for_session(
        self,
        org_id: UUID,
        session_id: UUID,
        project_id: UUID | None = None,
    ) -> int:
        """Count extractions for a session, optionally scoped to a project."""
        ...
