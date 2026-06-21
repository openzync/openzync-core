"""Session model — a conversational session scoped to a project.

Sessions group related episodes (messages) into a single conversation context.
Each project may have multiple sessions; the combination
``(project_id, external_id)`` is unique within a project. The ``user_id``
field tracks who created the session for attribution only — ownership is
at the project level.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class Session(TimestampMixin, Base):
    """A conversation session belonging to a project."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="The user who created this session (attribution only — ownership is via project).",
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
            "project_id",
            "external_id",
            name="uq_session_project_external",
        ),
        Index("ix_session_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<Session id={self.id} user={self.user_id} "
            f"active={self.is_active}>"
        )
