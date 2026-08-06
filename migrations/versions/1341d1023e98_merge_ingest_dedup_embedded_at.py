"""Merge ingest_dedup and embedded_at branches.

Two parallel migrations chained off ``0040`` — ``f1a4503b1f4c`` (adds the
``ingest_dedup`` table) and ``b7c9d1e2f3a4`` (adds ``facts.embedded_at``) —
left the tree with two heads, breaking ``alembic upgrade head``.  This
merge has no schema effect; it only reconciles the two branches under a
single head.

Revision ID: 1341d1023e98
Revises: f1a4503b1f4c, b7c9d1e2f3a4
Create Date: 2026-08-05
"""

from __future__ import annotations

# Alembic template convention: ``Sequence`` annotates branch_labels /
# depends_on but is never a runtime value.
from collections.abc import Sequence  # noqa: TC003

revision: str = "1341d1023e98"
down_revision: str | tuple[str, ...] | None = ("f1a4503b1f4c", "b7c9d1e2f3a4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op — both branches are already applied in their own migrations."""


def downgrade() -> None:
    """No-op — nothing to undo; the two branches each carry their own downgrade."""
