"""API key model — bearer-token authentication for programmatic access.

Keys are stored as salted hashes; only the ``prefix`` is stored in plaintext
for identification (e.g., ``mg_live_abc...``). The raw key is never persisted.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class ApiKey(TimestampMixin, Base):
    """An API key credential scoped to an organization.

    Attributes:
        id: UUID primary key.
        organization_id: Foreign key to the owning organization.
        key_hash: SHA-256 (or bcrypt) hash of the full API key. Unique.
        prefix: First few characters for identification — one of
            ``mg_live_`` or ``mg_test_``.
        name: Optional human-readable label for this key.
        scopes: Array of permission scopes (defaults to ``['read', 'write']``).
        last_used_at: Timestamp of most recent usage (updated on each request).
        expires_at: Optional expiration timestamp.
        is_revoked: Soft revocation flag.
    """

    __tablename__ = "api_keys"

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
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Optional project scope — NULL means org-wide access.",
    )
    lookup_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        comment="Unsalted SHA-256 of raw key — for fast DB/Redis lookups",
    )
    key_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Salted SHA-256 hash of raw key — for verification",
    )
    salt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="16-byte random hex salt used in key_hash computation",
    )
    prefix: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        default=["read", "write"],
        server_default="{read,write}",
    )
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    is_revoked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    __table_args__ = (
        CheckConstraint(
            "prefix IN ('mg_live_', 'mg_test_')",
            name="ck_api_key_prefix",
        ),
        Index("ix_api_key_organization_id", "organization_id"),
    )

    def __repr__(self) -> str:
        return f"<ApiKey id={self.id} prefix={self.prefix!r} revoked={self.is_revoked}>"
