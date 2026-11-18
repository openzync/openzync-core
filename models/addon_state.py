"""AddonState model — platform-global addon install registry.

One row per installed addon, regardless of organization.  Platform-wide
enablement only — there is no per-org tier.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class AddonState(TimestampMixin, Base):
    """Install/enablement state for one platform addon.

    Attributes:
        id: Human-readable addon id (matches ``addon.toml`` ``id``).
        version: Currently installed version (semver string).
        source_type: ``"local"`` or ``"github"``.
        source_ref: Local path or ``owner/repo`` for github.
        installed_sha: Git tag/SHA installed, or local mtime hash.
        latest_seen: Newest tag/version observed by the hourly check.
        enabled: Whether the addon is active platform-wide.
        update_available: Set by the hourly version check.
    """

    __tablename__ = "addon_states"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    installed_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_seen: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    update_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    generation: Mapped[int] = mapped_column(nullable=False, default=0)

    uid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
        unique=True,
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<AddonState id={self.id!r} v={self.version} enabled={self.enabled}>"
