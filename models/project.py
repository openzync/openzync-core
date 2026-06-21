"""Project model — collaborative workspace within an organization.

Projects group sessions, facts, graph knowledge, and configurations into
isolated workspaces. Each project has members with roles (owner, member)
and is scoped to a single organization for RLS enforcement.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class Project(TimestampMixin, Base):
    """A collaborative project workspace within an organization.

    Attributes:
        id: UUID primary key, generated server-side via gen_random_uuid().
        organization_id: Foreign key to the owning organization.
        name: Human-readable project name (unique within an organization).
        description: Optional longer description of the project's purpose.
        metadata_: Arbitrary JSONB metadata for extensible configuration.
        is_archived: Soft toggle — archived projects are hidden from default
            listing but data is preserved.
        created_by: Foreign key to the user who created this project.
            SET NULL on user deletion to preserve project continuity.
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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'metadata' is reserved by SQLAlchemy — use trailing underscore for the
    # Python attribute and map to the DB column via name="metadata".
    metadata_: Mapped[dict] = mapped_column(
        JSONB,
        name="metadata",
        nullable=False,
        default=dict,
        server_default="{}",
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "name",
            name="uq_projects_org_name",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Project id={self.id} org={self.organization_id} "
            f"name={self.name!r} archived={self.is_archived}>"
        )
