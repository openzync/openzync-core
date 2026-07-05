"""Fix api_keys prefix check constraint — mg_ → oz_ prefix values, align name with model.

The database constraint ``ck_api_keys_prefix`` expects ``mg_live_`` / ``mg_test_``
prefixes, but the entire codebase (``utils.crypto.generate_api_key``,
``services.api_key_service``, ``models.api_key.ApiKey``) uses
``oz_live_`` / ``oz_test_``.

This migration drops the stale constraint and creates a new one with the
correct prefix values and the name ``ck_api_key_prefix`` — matching the
definition in ``models/api_key.py``.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-05
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Drop the old constraint (wrong prefix values + wrong name) ─────────
    op.drop_constraint("ck_api_keys_prefix", "api_keys", type_="check")

    # ── Create the new constraint matching the model definition ────────────
    op.create_check_constraint(
        "ck_api_key_prefix",
        "api_keys",
        sa.text("prefix IN ('oz_live_', 'oz_test_')"),
    )


def downgrade() -> None:
    # ── Drop the corrected constraint ──────────────────────────────────────
    op.drop_constraint("ck_api_key_prefix", "api_keys", type_="check")

    # ── Restore the old (wrong) constraint ─────────────────────────────────
    op.create_check_constraint(
        "ck_api_keys_prefix",
        "api_keys",
        sa.text("prefix IN ('mg_live_', 'mg_test_')"),
    )
