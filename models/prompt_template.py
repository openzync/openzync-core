"""Prompt template model — versioned organization prompt templates.

All templates are org-scoped (``organization_id`` is always set).
System-level rows (``organization_id IS NULL``) no longer exist (Option A).
The source of truth for defaults is ``services/worker/prompts/manifest.yaml``
plus ``.jinja2`` files on disk, seeded at signup.
Only one template per (organization_id, template_name) can be active at a time.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class PromptTemplate(TimestampMixin, Base):
    """A versioned prompt template belonging either to a system or an organization.

    Attributes:
        id: UUID primary key.
        organization_id: FK to the owning organization (NULL for system defaults).
        template_name: Unique logical name within the scope (e.g. "memory_summary").
        template_text: The actual prompt text with ``{placeholder}`` variables.
        version: Monotonically increasing version number per (org, name).
        description: Human-readable description of the template's purpose.
        is_active: Whether this version is the active one for its scope.
    """

    __tablename__ = "prompt_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_default_for_type: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    @property
    def is_system_default(self) -> bool:
        """``True`` when this was seeded from the system manifest.

        Post-Option-A, all rows have ``organization_id`` set, so this
        always returns ``False``.  The property is retained for backward
        compatibility with the admin API response shape.
        """
        return self.organization_id is None and self.is_active

    def __repr__(self) -> str:
        return (
            f"<PromptTemplate id={self.id} name={self.template_name!r} "
            f"v{self.version} active={self.is_active}>"
        )
