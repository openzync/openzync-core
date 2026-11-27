"""Project member model — user membership and roles within a project.

Each row represents a single user's membership in a project with a specific
role. The combination (project_id, user_id) is unique — a user can only have
one row per project. New rows start as ``pending`` and become ``active``
only when the invitee approves (or a platform admin approves for them);
existing rows were backfilled to ``active``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin
from models.user import User


class ProjectMember(TimestampMixin, Base):
    """A user's membership and role within a project.

    Attributes:
        id: UUID primary key, generated server-side via gen_random_uuid().
        project_id: Foreign key to the project.
        user_id: Foreign key to the user.
        role: Access level — ``owner`` (manage project settings and members)
            or ``member`` (read/write access to project data).
        status: ``pending`` until the invitee approves, then ``active``.
            Only ``active`` rows grant project access.
    """

    __tablename__ = "project_members"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        # index defined explicitly in __table_args__ below
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="member",
        server_default="member",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
        server_default="active",
    )
    user: Mapped[User | None] = relationship(
        lazy="raise",
        # note: read-only eager target for list_members' LEFT JOIN
        # (contains_eager). ``raise`` fails loud on accidental lazy load
        # in async context instead of raising MissingGreenlet at runtime.
        # No is_deleted filter here — soft-deleted users still resolve.
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "user_id",
            name="uq_project_members_project_user",
        ),
        Index("ix_project_members_user_id", "user_id"),
        Index("ix_project_members_project_id", "project_id"),
        CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_project_members_role",
        ),
        CheckConstraint(
            "status IN ('pending', 'active')",
            name="ck_project_members_status",
        ),
    )

    def __repr__(self) -> str:
        """Return a debug-friendly repr with project, user, role, status."""
        return (
            f"<ProjectMember project={self.project_id} "
            f"user={self.user_id} role={self.role!r} status={self.status!r}>"
        )
