"""Organization model — inert legacy tenant entity.

Kept only so the ``organizations`` table stays mapped: migration 0070
drops only the dead join-code policies, NOT this table. The table drop
is deferred beyond 0070. No new code may reference this model —
ownership is user-scoped (RLS ``user_isolation`` policies keyed on
``app.user_id``).
"""

import uuid

from sqlalchemy import Boolean, CheckConstraint, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.config import PLATFORM_ORG_ID
from models.base import Base, TimestampMixin


class Organization(TimestampMixin, Base):
    """A tenant organization in the OpenZync platform.

    Single platform org — there is no ``DELETE /organizations`` endpoint
    and no service-layer delete; all ``organizations.id`` FKs are
    ``ON DELETE RESTRICT`` so a stray SQL delete fails loudly instead of
    wiping tenant rows.

    Attributes:
        id: UUID primary key, generated server-side via gen_random_uuid().
        name: Human-readable organization name.  The platform org is always
            named ``SYSTEM`` (exact match — see :attr:`is_platform`).
        status: Lifecycle state — one of ``pending``, ``approved``,
            ``rejected``.  ``pending`` orgs await superadmin approval and
            are excluded from every tenant-facing lookup.
        config: JSONB blob for all per-org configuration (LLM, embeddings,
            graph, behaviour).  UI-exposed.  ``None`` fields fall back to
            env-var defaults from ``core.config.settings``.
        quotas: JSONB blob for usage quotas (max_sessions, max_episodes, etc.).
        is_active: Soft toggle for deactivation.
    """

    __tablename__ = "organizations"  # inert: kept mapped; drop deferred beyond 0070.

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Org lifecycle — retained for history.  Single platform org: only
    # ``approved`` rows exist; status is kept for the quarantine audit trail.
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="approved",
        server_default="approved",
    )
    config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Per-org UI-exposed configuration (LLM, embeddings, graph, behaviour).",
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
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_organization_status",
        ),
    )

    @property
    def is_platform(self) -> bool:
        """Derived platform marker — the platform org is the fixed-ID row.

        Compares ``id`` against ``PLATFORM_ORG_ID`` (renames do not
        affect identity — the old ``name == "SYSTEM"`` check went stale
        the moment bootstrap naming became operator-chosen).
        """
        return self.id == PLATFORM_ORG_ID

    def __repr__(self) -> str:
        return (
            f"<Organization id={self.id} name={self.name!r} "
            f"status={self.status}>"
        )
