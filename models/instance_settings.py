"""Instance settings model — single-row platform-level configuration.

Holds platform-wide state that no organization owns: registration mode,
initialization flag, the SHA-256 hash of the bootstrap token (never the
plaintext), and default graph/LLM backends applied to new organizations.

Exactly one row exists (fixed PK ``id = 1``), seeded by migration 0029.
The repository's ``get()`` recreates it lazily if it is ever missing.
No RLS — this table has no ``organization_id`` column.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class InstanceSettings(Base):
    """Platform-wide settings singleton row.

    Attributes:
        id: Fixed primary key (``1``) — enforces single-row semantics.
        registration_mode: One of ``disabled``, ``enabled_with_org_code``,
            ``enabled_self_serve_org_creation``.
        initialized: Whether the instance setup wizard has completed.
        bootstrap_token_hash: SHA-256 hex digest of the one-time bootstrap
            token.  The plaintext is printed once at boot and written to
            ``/openbao-bootstrap/bootstrap-token`` — never stored here.
        default_backends: JSONB ``{"llm": {...}|null, "graph": "postgres"|...}``
            applied to orgs created by the setup wizard.
        updated_at: Last write timestamp.
    """

    __tablename__ = "instance_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    registration_mode: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="enabled_self_serve_org_creation",
        server_default="enabled_self_serve_org_creation",
    )
    initialized: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    bootstrap_token_hash: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="SHA-256 hex digest of the one-time bootstrap token (never the plaintext).",
    )
    default_backends: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    updated_at: Mapped[datetime] = mapped_column(
        func.now(),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "registration_mode IN "
            "('disabled', 'enabled_with_org_code', 'enabled_self_serve_org_creation')",
            name="ck_instance_settings_registration_mode",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<InstanceSettings id={self.id} initialized={self.initialized} "
            f"registration_mode={self.registration_mode!r}>"
        )
