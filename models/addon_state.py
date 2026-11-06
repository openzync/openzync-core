"""Addon lifecycle state — one row per installed addon.

Platform-scoped (no ``organization_id``): addons are installed, enabled,
and upgraded by the platform super-admin for the whole instance. No RLS
policies apply — this table intentionally carries no tenant column.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class AddonState(TimestampMixin, Base):
    """Install/enable state for one addon.

    Attributes:
        id: The addon slug (matches ``addon.toml`` ``name``).
        version: The currently loaded version.
        enabled: Whether routes/tasks serve traffic (``False`` → 403).
        status: Lifecycle marker: installed/enabled/disabled/error.
        manifest: The validated manifest snapshot at install time.
        settings_schema: The validated ``settings.schema.json`` snapshot,
            or ``None`` when the addon declares no settings.
    """

    __tablename__ = "addon_states"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="installed")
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    settings_schema: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
