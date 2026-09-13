"""Ingest dedup repository — atomic content-dedup claims.

Backs batch-level content deduplication in the memory service.  The claim
method uses PostgreSQL ``INSERT ... ON CONFLICT DO NOTHING`` against the
unique ``(project_id, session_id, content_hash)`` index, so exactly one of
two concurrent identical submissions wins inside the caller's transaction —
the loser's INSERT returns zero rows instead of erroring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.ingest_dedup import IngestDedup

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

# ╠ This file contains NO business logic — only query construction.


class IngestDedupRepository:
    """All database access for the ``ingest_dedup`` table.

    No business logic — pure query construction and execution.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def insert_or_none(
        self,
        project_id: UUID,
        session_id: UUID,
        content_hash: str,
        job_id: UUID,
    ) -> bool:
        """Atomically claim a content triple, returning False on duplicate.

        Args:
            project_id: The project UUID scoping the claim.
            session_id: The session UUID scoping the claim.
            content_hash: The SHA-256 batch content hash.
            job_id: The job UUID of the accepted ingest being claimed.

        Returns:
            ``True`` if this call inserted the row (claim won), ``False``
            if the row already existed (duplicate).
        """
        stmt = (
            pg_insert(IngestDedup)
            .values(
                job_id=job_id,
                project_id=project_id,
                session_id=session_id,
                content_hash=content_hash,
            )
            .on_conflict_do_nothing(
                index_elements=["project_id", "session_id", "content_hash"],
            )
            .returning(IngestDedup.job_id)
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get_job_id(
        self,
        project_id: UUID,
        session_id: UUID,
        content_hash: str,
    ) -> UUID | None:
        """Return the job UUID of the accepted ingest for a content triple.

        Used on the duplicate path (claim lost to a concurrent request) to
        reference the prior ``job_id`` in the response, preserving the
        pre-fix duplicate response shape.

        Args:
            project_id: The project UUID scoping the claim.
            session_id: The session UUID scoping the claim.
            content_hash: The SHA-256 batch content hash.

        Returns:
            The prior ``job_id`` if a claim exists, else ``None``.
        """
        stmt = (
            select(IngestDedup.job_id)
            .where(
                IngestDedup.project_id == project_id,
                IngestDedup.session_id == session_id,
                IngestDedup.content_hash == content_hash,
            )
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()
