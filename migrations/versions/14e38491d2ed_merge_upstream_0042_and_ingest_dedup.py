"""Merge upstream and ingest-dedup branches.

A rebase joined two histories that both branched off ``0040``: upstream
``0041``/``0042`` (partial index on ``facts.active_spo``) and our
``f1a4503b1f4c`` + ``b7c9d1e2f3a4`` merged by ``1341d1023e98`` (ingest
dedup table, ``facts.embedded_at``) — leaving two heads and breaking
``alembic upgrade head``.  This merge has no schema effect; it only
reconciles the two branches under a single head.

Revision ID: 14e38491d2ed
Revises: 0042, 1341d1023e98
Create Date: 2026-08-06
"""

from __future__ import annotations

# Alembic template convention: ``Sequence`` annotates branch_labels /
# depends_on but is never a runtime value.
from collections.abc import Sequence  # noqa: TC003

revision: str = "14e38491d2ed"
down_revision: str | tuple[str, ...] | None = ("0042", "1341d1023e98")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op — both branches are already applied in their own migrations."""


def downgrade() -> None:
    """No-op — nothing to undo; the two branches each carry their own downgrade."""
