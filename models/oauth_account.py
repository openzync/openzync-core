"""OAuthAccount model — links external OAuth provider identities to dashboard users.

Each row maps one OAuth provider identity (e.g. a Google account) to exactly
one dashboard user. A user can have multiple OAuthAccount rows (e.g. both
Google and GitHub linked to the same account).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class OAuthAccount(TimestampMixin, Base):
    """A linked OAuth provider identity for a dashboard user.

    Attributes:
        id: UUID primary key.
        provider: OAuth provider name (``"google"`` or ``"github"``).
        provider_user_id: The user's unique ID from the OAuth provider.
        user_id: Foreign key to the dashboard ``User``.
    """

    __tablename__ = "oauth_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="OAuth provider name: 'google' or 'github'.",
    )
    provider_user_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The user's unique ID from the OAuth provider.",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="Foreign key to the dashboard user.",
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_user_id",
            name="uq_oauth_provider_user",
        ),
        Index("ix_oauth_accounts_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<OAuthAccount id={self.id} provider={self.provider!r} "
            f"provider_user_id={self.provider_user_id!r} user_id={self.user_id}>"
        )
