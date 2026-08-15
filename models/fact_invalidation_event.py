"""FactInvalidationEvent model — audit trail for fact-invalidation lineage.

Every way a fact stops being current — superseded by a conflicting fact,
manually retracted, LLM-invalidated, or time-expired — records one row here
so the lineage of a fact (and its successors) can be reconstructed.

Append-only: rows are never updated or deleted; ``updated_at`` exists only
because the table inherits :class:`~models.base.TimestampMixin` for
consistency with sibling models.

Table ``fact_invalidation_events`` is created via migration 0048.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class FactInvalidationEvent(TimestampMixin, Base):
    """A single fact-invalidation record in the lineage audit trail.

    Attributes:
        id: UUID primary key.
        organization_id: Denormalized organization ID for RLS — no FK,
            mirrors ``facts.organization_id``.
        project_id: Project scope, FK to ``projects`` (CASCADE).
        old_fact_id: The fact that stopped being current (FK to
            ``facts``, SET NULL on delete).
        new_fact_id: The fact that replaced it, if any — NULL for
            retractions/expiry (FK to ``facts``, SET NULL on delete).
        kind: One of ``"superseded" | "retracted" | "llm_invalidated" |
            "time_expired"`` — app-enforced; the DB CHECK constraint
            lives in migration 0048.
        reason: Optional human/LLM-supplied reason for the invalidation.
        at_time: Instant the invalidation took effect.
        source_episode_id: Episode that drove the invalidation (e.g. the
            episode whose conflicting fact superseded this one).
        created_at: Row creation timestamp.
        updated_at: Row last-update timestamp.
    """

    __tablename__ = "fact_invalidation_events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        # No FK constraint — denormalized for RLS performance, mirrors facts.
        # ⚠️ data integrity is application-enforced, not DB-enforced
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    old_fact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("facts.id", ondelete="SET NULL"),
        nullable=False,
        index=True,
    )
    new_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("facts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "One of 'superseded' | 'retracted' | 'llm_invalidated' | "
            "'time_expired' — app-enforced; DB CHECK in migration 0048."
        ),
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    at_time: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
    )
    source_episode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_fact_invalidation_events_old_fact_id_at_time",
            "old_fact_id",
            "at_time",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FactInvalidationEvent id={self.id} old_fact_id={self.old_fact_id} "
            f"kind={self.kind!r}>"
        )
