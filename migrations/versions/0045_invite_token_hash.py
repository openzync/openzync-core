"""Add ``users.invite_token_hash`` — the admin invite flow.

The hash backs the "admin invites user by email → invitee sets password via
magic link" flow.  A non-NULL ``invite_token_hash`` marks a pending invite:
the user row exists with ``password_hash = NULL`` and the raw magic-link
token is delivered by email (only its SHA-256 hash is stored).

- Column is NULLable — accepted/revoked users and ordinary signup/join
  users carry NULL.
- Partial index matches the ``claim_invite`` UPDATE (lookup by hash) while
  keeping NULL rows out of the index.
- No data backfill and no new table — no RLS session statements are needed
  (unlike 0044, which had to bypass RLS for its backfill UPDATE on
  ``organizations``).

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable ``invite_token_hash`` column and its partial index."""
    op.add_column(
        "users",
        sa.Column("invite_token_hash", sa.TEXT(), nullable=True),
    )
    op.create_index(
        "ix_user_invite_token_hash",
        "users",
        ["invite_token_hash"],
        postgresql_where=sa.text("invite_token_hash IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the partial index and the ``invite_token_hash`` column."""
    op.drop_index("ix_user_invite_token_hash", table_name="users")
    op.drop_column("users", "invite_token_hash")
