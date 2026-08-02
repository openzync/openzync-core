"""Add partial btree index on active facts (project_id, subject, predicate, object).

Backs the supersession conflict scan: finding active facts sharing a new
fact's SPO identity within a project.  The index is **partial**
(``WHERE invalid_at IS NULL``) so hard-retracted facts never pollute it,
and it deliberately includes superseded rows (``valid_to`` set but
``invalid_at`` NULL) — the conflict scan must see those to decide
whether the incoming fact replaces them.

    CREATE INDEX CONCURRENTLY ix_facts_active_spo
        ON facts (project_id, subject, predicate, object)
        WHERE invalid_at IS NULL;

``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction, so the
migration commits Alembic's implicit transaction before creating the
index and re-opens one afterwards for the version-table update.  If the
index creation fails, the migration is not recorded and can be re-run.

Rollback note:
    ``DROP INDEX CONCURRENTLY IF EXISTS ix_facts_active_spo`` — safe,
    only drops the index, no data is touched.  The supersession conflict
    scan falls back to a sequential scan until the index is restored.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-02
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_facts_active_spo"


def upgrade() -> None:
    # ── 1. Leave Alembic's transaction — CONCURRENTLY is non-transactional ──
    op.execute("COMMIT")
    op.execute(
        f"CREATE INDEX CONCURRENTLY {_INDEX_NAME} "
        "ON facts (project_id, subject, predicate, object) "
        "WHERE invalid_at IS NULL"
    )
    op.execute("BEGIN")


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    op.execute("BEGIN")
