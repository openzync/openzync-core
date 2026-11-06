"""Per-org addon state — enable flag plus plain (non-secret) settings values.

Org-scoped (``organization_id``): RLS applies normally via ``get_db`` —
each org sees only its own rows. Secret setting values never land here;
they live in OpenBao under the org namespace (see
:class:`services.addon_org_service.AddonOrgService`).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class AddonOrgSettings(TimestampMixin, Base):
    """One org's view of one addon: enabled flag + plain settings values.

    Attributes:
        addon_id: The addon slug (plain text — no FK: uninstall drops the
            platform row while org rows simply go dormant).
        organization_id: The owning org (RLS tenant column).
        enabled: Per-org flag; effective only when the platform flag is on.
        values: Non-secret settings values (secrets live in OpenBao).
    """

    __tablename__ = "addon_org_settings"

    addon_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    values: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
