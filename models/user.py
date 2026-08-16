"""User model — represents an end-user within an organization.

Users are identified by an ``external_id`` chosen by the calling application
(e.g., a UUID from the customer's auth system). The combination
``(organization_id, external_id)`` is unique.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
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
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="member",
        server_default="member",
    )
    password_hash: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="bcrypt hash — set only for dashboard users (email/password auth).",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    # First-login password reset gate — True for the seeded root user (whose
    # password comes from the OZ_ROOT_PASSWORD default) until they set a
    # real password.  Enforced in dependencies/auth.py get_dashboard_user.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    summary_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    is_email_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    invite_token_hash: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "SHA-256 hash of the pending admin-invite token.  Non-NULL means "
            "this user was invited but has not yet accepted; NULL after accept "
            "or revoke."
        ),
    )
    locale: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="en",
        server_default="en",
        comment=(
            "BCP-47 locale tag (lowercase, e.g. 'en', 'de') — selects the "
            "language of transactional emails.  Must be in "
            "core.locales.ALLOWED_LOCALES."
        ),
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "external_id",
            name="uq_user_organization_external",
        ),
        Index("ix_user_organization_id", "organization_id"),
        Index("ix_user_email_unique", "email", postgresql_where=text("email IS NOT NULL AND is_deleted = false")),
        Index(
            "ix_user_invite_token_hash",
            "invite_token_hash",
            postgresql_where=text("invite_token_hash IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} org={self.organization_id} "
            f"external={self.external_id!r}>"
        )
