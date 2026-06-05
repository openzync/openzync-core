"""Session model — a conversational session owned by a user.

Sessions group related episodes (messages) into a single conversation context.
Each user may have multiple sessions; the combination ``(user_id, external_id)``
is unique within a user.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class Session(TimestampMixin, Base):
    """A conversation session belonging to a user.

    Attributes:
        id: UUID primary key.
        user_id: Foreign key to the owning user.
        external_id: Caller-chosen session identifier
            (e.g., ``conv-20240601-abc``).
        metadata: Arbitrary JSONB metadata.
        is_active: Whether the session is currently accepting new episodes.
        is_deleted: Soft-delete flag.
        closed_at: Timestamp when the session was explicitly closed.
    """

    __tablename__ = "sessions"

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
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 'metadata' is reserved by SQLAlchemy — use trailing underscore for the
    # Python attribute and map to the DB column via name="metadata".
    metadata_: Mapped[dict] = mapped_column(
        JSONB,
        name="metadata",
        nullable=False,
        default=dict,
        server_default="{}",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "external_id",
            name="uq_session_user_external",
        ),
        Index("ix_session_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<Session id={self.id} user={self.user_id} "
            f"active={self.is_active}>"
        )
