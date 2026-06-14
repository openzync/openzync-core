"""Custom instruction model — domain-specific text snippets for extraction guidance.

Each row represents a named instruction scoped to an organization, extraction
domain, and optional target entity (user UUID for ``user_summary`` scope).
Instructions are injected into LLM prompts to guide extraction behavior.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class CustomInstruction(TimestampMixin, Base):
    """A named instruction snippet guiding LLM extraction behavior.

    Attributes:
        id: UUID primary key, generated server-side.
        organization_id: Foreign key to the owning organization.
        scope: Instruction scope — ``extraction`` or ``user_summary``.
        target_id: Optional target entity UUID (e.g. user UUID for
            ``user_summary``).  ``NULL`` represents org-level instructions.
        name: Human-readable label (e.g. ``legal_domain``, ``healthcare``).
        text: The instruction text content injected into extraction prompts.
    """

    __tablename__ = "custom_instructions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        target = f"target={self.target_id}" if self.target_id else "org-level"
        return (
            f"<CustomInstruction id={self.id} name={self.name!r} "
            f"scope={self.scope!r} {target}>"
        )
