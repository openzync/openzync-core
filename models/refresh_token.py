"""Refresh token model — durable credentials for session renewal.

Refresh tokens enable long-lived sessions without storing the access token.
They form a rotation chain: each rotation creates a new token and invalidates
the previous one, linked via ``rotated_by``.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class RefreshToken(TimestampMixin, Base):
    """A refresh token for admin/user session renewal.

    Attributes:
        id: UUID primary key.
        user_id: Admin user identifier (external — caller-chosen).
        organization_id: Foreign key to the owning organization.
        token_hash: Hashed refresh token value. Unique.
        expires_at: Expiration timestamp — tokens past this date are rejected.
        is_revoked: Soft revocation flag.
        rotated_by: UUID of the ``RefreshToken`` that replaced this one.
            Forms a chain for auditing rotation history.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
    )
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    is_revoked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    rotated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:
        return (
            f"<RefreshToken id={self.id} "
            f"revoked={self.is_revoked} expires={self.expires_at}>"
        )
