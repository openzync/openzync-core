"""Fact model — extracted knowledge triplet from conversation episodes.

Facts represent structured knowledge in subject-predicate-object form,
optionally linked back to the source episode. Multiple facts can be extracted
from a single episode via the enrichment pipeline.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Float,
    ForeignKey,
    Index,
    TIMESTAMP,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class Fact(TimestampMixin, Base):
    """An extracted knowledge fact about an entity or relationship.

    Attributes:
        id: UUID primary key.
        user_id: Foreign key to the owning user.
        organization_id: Denormalized organization ID for RLS enforcement.
        content: Human-readable fact statement (e.g., "Alice likes hiking").
        subject: The subject of the triple (e.g., "Alice").
        predicate: The relationship/predicate (e.g., "likes").
        object: The object of the triple (e.g., "hiking").
        subject_type: Entity type of the subject (default ``literal``).
        object_type: Entity type of the object (default ``literal``).
        confidence: Extraction confidence score (0.0–1.0).
        source_episode_id: Optional FK back to the originating episode.
        valid_from: Temporal validity start.
        valid_to: Temporal validity end.
        invalid_at: Timestamp when this fact was invalidated/retracted.
        embedding: pgvector embedding placeholder (migrated to
            ``vector(1536)`` via Alembic).
    """

    __tablename__ = "facts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        # index defined explicitly in __table_args__ below
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        # No FK constraint — this is denormalized for RLS performance
        # Must be kept in sync with the user's organization_id at write time
        # ⚠️ data integrity is application-enforced, not DB-enforced
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    predicate: Mapped[str | None] = mapped_column(Text, nullable=True)
    object: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="literal",
        server_default="literal",
    )
    object_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="literal",
        server_default="literal",
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
    )
    source_episode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    valid_from: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    invalid_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    # TechLead note: Text is a stand-in for ``vector(1536)``. The Alembic
    # migration will alter this column when pgvector is available.
    embedding: Mapped[list[float] | None] = mapped_column(
        ARRAY(Float), nullable=True, default=None,
    )

    __table_args__ = (
        Index("ix_fact_user_id", "user_id"),
        Index("ix_fact_user_valid_range", "user_id", "valid_from", "valid_to"),
    )

    def __repr__(self) -> str:
        return (
            f"<Fact id={self.id} subject={self.subject!r} "
            f"predicate={self.predicate!r} object={self.object!r}>"
        )
