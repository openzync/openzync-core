"""Project model — logical grouping between Organization and Sessions/Graph.

A project is a workspace within an organization. Users are added to projects
via the ``project_members`` join table with a role. Sessions and graph entities
live under a project, not directly under the organization.

The hierarchy is::

    Organization
        └── Project
            ├── ProjectMember (user_id, role)
            ├── Session → Episode → Fact
            └── GraphEntity / GraphRelationship
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class Project(TimestampMixin, Base):
    """A project / workspace within an organization.

    Attributes:
        id: UUID primary key.
        organization_id: FK to the owning organization.
        name: Human-readable project name.
        description: Optional longer description.
        is_active: Soft toggle for deactivation.
        is_deleted: Soft-delete flag.
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
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

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "name",
            name="uq_project_org_name",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Project id={self.id} name={self.name!r} "
            f"org={self.organization_id}>"
        )


class ProjectMember(TimestampMixin, Base):
    """Many-to-many join table linking users to projects with a role.

    Attributes:
        id: UUID primary key.
        project_id: FK to the project.
        user_id: FK to the user.
        role: One of ``admin``, ``member``, or ``viewer``.
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
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="member",
        server_default="member",
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "user_id",
            name="uq_project_member",
        ),
        CheckConstraint(
            "role IN ('admin', 'member', 'viewer')",
            name="ck_project_member_role",
        ),
        # Composite index for the common query: "find all members of a project"
        # Included automatically by the PK + FK indexes, but make intent explicit:
        # Index("ix_project_members_project", "project_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<ProjectMember project={self.project_id} "
            f"user={self.user_id} role={self.role!r}>"
        )
