"""Episode model — a single message turn within a conversation session.

Episodes are ordered by ``sequence_number`` within a session. Each episode
captures the role (user, assistant, system, tool), content, optional embedding,
and enrichment metadata (e.g., extracted facts, classifications).
"""

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class Episode(TimestampMixin, Base):
    """A single message / turn within a session.

    Attributes:
        id: UUID primary key.
        session_id: Foreign key to the parent session.
        user_id: Denormalized foreign key to the user (enables fast lookups
            without joining through sessions).
        role: Message role — one of ``user``, ``assistant``, ``system``, ``tool``.
        content: Message body text. Max length 65536 characters.
        metadata: Arbitrary JSONB metadata.
        embedding: pgvector embedding (placeholder — migrated to ``vector(1536)``
            via Alembic). Nullable; populated after enrichment.
        token_count: Approximate token count for the message.
        sequence_number: Order within the session (0-based).
        enrichment_status: Bitmask tracking which enrichment passes have been
            completed (e.g., bit 0 = fact extraction, bit 1 = classification,
            bit 2 = summarization).
        is_deleted: Soft-delete flag.
    """

    __tablename__ = "episodes"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Denormalized for efficient project-scoped queries without joining through session.",
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        # index defined explicitly in __table_args__ below
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 'metadata' is reserved by SQLAlchemy — use trailing underscore for the
    # Python attribute and map to the DB column via name="metadata".
    metadata_: Mapped[dict] = mapped_column(
        JSONB,
        name="metadata",
        nullable=False,
        default=dict,
        server_default="{}",
    )
    # note: Embedding uses Text as a stand-in type because pgvector
    # may not be installed in the dev/test environment. The actual DDL must
    # use ``vector(1536)`` — the Alembic migration will handle this.
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    sequence_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    enrichment_status: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="ck_episode_role",
        ),
        CheckConstraint(
            "char_length(content) <= 65536",
            name="ck_episode_content_length",
        ),
        Index("ix_episode_session_sequence", "session_id", "sequence_number"),
        Index("ix_episode_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<Episode id={self.id} session={self.session_id} "
            f"seq={self.sequence_number} role={self.role!r}>"
        )
