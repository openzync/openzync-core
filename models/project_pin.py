"""Project pin model — per-user dashboard pin for a project.

Pins are a user preference, not membership: a user may pin at most
``MAX_PINS`` (3) projects per organization. Rows are deleted when the user,
project, or organization is deleted (CASCADE on all three FKs).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class ProjectPin(TimestampMixin, Base):
    """A user's pin on a project within an organization.

    Attributes:
        id: UUID primary key, generated server-side via gen_random_uuid().
        user_id: Foreign key to the user who pinned. CASCADE on delete.
        project_id: Foreign key to the pinned project. CASCADE on delete.
        organization_id: Tenant scope. CASCADE on delete.
        pinned_at: When the pin was created. Domain ordering key for
            ``pinned_only`` listing (most recent first). Kept distinct
            from ``created_at`` so pin ordering never shifts on row updates.
    """

    __tablename__ = "project_pins"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    pinned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            name="uq_project_pins_user_project",
        ),
        Index(
            "ix_project_pins_user_org_pinned", "user_id", "organization_id", "pinned_at"
        ),
        Index("ix_project_pins_project_id", "project_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<ProjectPin user={self.user_id} project={self.project_id} "
            f"org={self.organization_id} pinned_at={self.pinned_at}>"
        )
