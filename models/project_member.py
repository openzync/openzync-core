"""Project member model — user membership and roles within a project.

Each row represents a single user's membership in a project with a specific
role. The combination (project_id, user_id) is unique — a user can only have
one role per project.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class ProjectMember(TimestampMixin, Base):
    """A user's membership and role within a project.

    Attributes:
        id: UUID primary key, generated server-side via gen_random_uuid().
        project_id: Foreign key to the project.
        user_id: Foreign key to the user.
        role: Access level — ``owner`` (manage project settings and members)
            or ``member`` (read/write access to project data).
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
    )

    def __repr__(self) -> str:
        return (
            f"<ProjectMember project={self.project_id} "
            f"user={self.user_id} role={self.role!r}>"
        )
