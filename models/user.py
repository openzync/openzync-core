"""User model — represents an end-user within an organization.

Users are identified by an ``external_id`` chosen by the calling application
(e.g., a UUID from the customer's auth system). The combination
``(organization_id, external_id)`` is unique.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    """An end-user scoped to an organization.

    Attributes:
        id: UUID primary key.
        organization_id: Foreign key to the owning organization.
        external_id: Caller-chosen identifier for this user
            (e.g., ``customer-abc-123``).
        name: Optional display name.
        email: Optional email address.
        metadata: Arbitrary JSONB metadata.
        is_active: Soft toggle for deactivation.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        # index defined explicitly in __table_args__ below
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "external_id",
            name="uq_user_organization_external",
        ),
        Index("ix_user_organization_id", "organization_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} org={self.organization_id} "
            f"external={self.external_id!r}>"
        )
