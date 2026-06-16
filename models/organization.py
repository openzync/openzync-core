"""Organization model — top-level tenant entity.

Each organization owns users, API keys, extraction schemas, and billing config.
Isolation between organizations is enforced via RLS policies keyed on
``organization_id`` throughout the schema.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class Organization(TimestampMixin, Base):
    """A tenant organization in the OpenZep platform.

    Attributes:
        id: UUID primary key, generated server-side via gen_random_uuid().
        name: Human-readable organization name.
        plan: Billing plan — one of ``free``, ``pro``, ``enterprise``.
        config: JSONB blob for all per-org configuration (LLM, embeddings,
            graph, behaviour).  UI-exposed.  ``None`` fields fall back to
            env-var defaults from ``core.config.settings``.
        llm_config: **Deprecated** — kept for backward compatibility during
            migration.  Reads/writes alias to ``config->'llm'``.  Prefer
            ``config`` for new code.
        quotas: JSONB blob for usage quotas (max_sessions, max_episodes, etc.).
        is_active: Soft toggle for deactivation.
    """

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="free",
        server_default="free",
    )
    config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Per-org UI-exposed configuration (LLM, embeddings, graph, behaviour).",
    )
    # DEPRECATED: llm_config is kept for data migration.  New code should use
    # the ``config`` column.  The Alembic migration 0002 copies existing
    # llm_config values into config->'llm'.
    llm_config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    quotas: Mapped[dict] = mapped_column(
        JSONB,
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

    __table_args__ = (
        CheckConstraint(
            "plan IN ('free', 'pro', 'enterprise')",
            name="ck_organization_plan",
        ),
    )

    def __repr__(self) -> str:
        return f"<Organization id={self.id} name={self.name!r} plan={self.plan}>"
