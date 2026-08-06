"""Ingest dedup model — atomic content-dedup claims for batch ingestion.

Each row records that a ``(project_id, session_id, content_hash)`` batch was
accepted, storing the ``job_id`` of the accepted ingest.  The unique index on
that triple makes concurrent identical submissions serialize at the database
level, closing the check-then-store TOCTOU window that Redis-only dedup left
open (the Redis key was written only after the DB transaction committed).
"""

from __future__ import annotations

# SQLAlchemy must resolve the ``Mapped[uuid.UUID]`` annotation string at
# runtime — ``uuid`` cannot live in a TYPE_CHECKING-only block.
import uuid  # noqa: TC003

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, CreatedAtMixin


class IngestDedup(CreatedAtMixin, Base):
    """A claim that a content batch has been accepted for ingestion.

    Append-only — claims are never updated.  ``job_id`` is the primary key
    and is NOT server-generated: the memory service generates it before the
    claim so it can be referenced by the duplicate response and the ARQ
    enrichment tasks.

    Attributes:
        job_id: Primary key — the job UUID of the accepted ingest.
        project_id: The project UUID scoping the claim.
        session_id: The session UUID scoping the claim.
        content_hash: SHA-256 hash of the full messages batch.
    """

    __tablename__ = "ingest_dedup"

    job_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index(
            "uq_ingest_dedup_project_session_hash",
            "project_id",
            "session_id",
            "content_hash",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        """Return a compact, debug-friendly representation of the claim."""
        return (
            f"<IngestDedup job={self.job_id} project={self.project_id} "
            f"session={self.session_id} hash={self.content_hash[:12]}>"
        )
