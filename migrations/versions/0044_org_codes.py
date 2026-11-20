"""Add ``organizations.org_code`` — the org join code.

The org code is the token a new member presents at ``POST /v1/auth/join``
to join an existing organization.  Stored **plaintext by explicit product
decision** — it grants membership only, not billing or system access, and
is rotated via ``POST /admin/org/org-code/regenerate``.

Backfill: every existing organization receives a fresh random code, then
the column is set NOT NULL with a unique index.  Codes are 8 chars from a
confusion-free alphabet (``core/org_codes``).

NOTE (single platform org): join codes are dead — ``POST /v1/auth/join``
and signup are governed by ``instance_settings.registration_mode`` and
``repositories`` mint a random placeholder for the legacy NOT NULL
column.  The column stays for the constraint only; see 0067.

RLS: the ``organizations`` table carries row-level security policies keyed
on ``app.bypass_rls``/``app.org_id`` (migrations/0001).  The migration sets
``app.bypass_rls = 'true'`` session-locally so the backfill UPDATE is not
silently filtered when the migration role is not the table owner.

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-08
"""

from __future__ import annotations

import secrets

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | None = None
depends_on: str | None = None

ORG_CODE_ALPHABET: str = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""Confusion-free alphabet — excludes I, O, L, 0, 1 (was ``core.org_codes``)."""

ORG_CODE_LENGTH: int = 8
"""Length of generated org codes (in characters)."""


def generate_org_code() -> str:
    """Generate a fresh 8-char join code.

    Inlined copy of the deleted ``core.org_codes.generate_org_code`` —
    migrations must be self-contained and never import app code, which
    evolves while history must not. Behavior identical: ``secrets.choice``
    over the confusion-free alphabet.
    """
    return "".join(secrets.choice(ORG_CODE_ALPHABET) for _ in range(ORG_CODE_LENGTH))


def upgrade() -> None:
    """Add ``org_code``, backfill every row, then enforce NOT NULL + unique."""
    op.add_column(
        "organizations",
        sa.Column("org_code", sa.VARCHAR(24), nullable=True),
    )

    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")

    # Backfill: one UPDATE per row so every existing org gets a random code.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id FROM organizations"))
    for row in rows:
        bind.execute(
            sa.text("UPDATE organizations SET org_code = :code WHERE id = :id"),
            {"code": generate_org_code(), "id": row[0]},
        )

    op.alter_column("organizations", "org_code", nullable=False)
    op.create_index(
        "uq_organizations_org_code",
        "organizations",
        ["org_code"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the unique index and the ``org_code`` column."""
    op.drop_index("uq_organizations_org_code", table_name="organizations")
    op.drop_column("organizations", "org_code")
