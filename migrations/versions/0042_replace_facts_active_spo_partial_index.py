"""Replace the unusable active-SPO partial index with a usable expression index.

Revision 0041 created ``ix_facts_active_spo`` — a partial btree on
``(project_id, subject, predicate, object) WHERE invalid_at IS NULL`` —
to back the supersession conflict scan.  That index is dead weight
(pure write overhead): the scan in
``fact_repository.find_conflicting_active_for_update`` filters string
keys with ``func.lower(Fact.subject)`` / ``func.lower(Fact.object)``
(no expression index serves ``lower()``), and entity-UUID keys filter
on ``subject_entity_id`` / ``object_entity_id`` (columns not in the
index).  The effective-at clause (``invalid_at IS NULL OR invalid_at > t``
etc.) also does not imply the partial predicate ``invalid_at IS NULL``,
so the partial index can omit rows the scan should match.

    CREATE INDEX CONCURRENTLY ix_facts_active_spo_expr
        ON facts (project_id, lower(subject), lower(predicate), lower(object));

The leading column ``project_id`` matches the scan's project equality
filter and the ``lower(...)`` columns serve its case-insensitive
key comparisons.  Entity-UUID key lookups are serialized by the advisory
xact lock (added separately for fact ingestion) and do not need an
index.

``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction, so the
migration commits Alembic's implicit transaction around each index
statement and re-opens one afterwards for the version-table update —
mirroring the workaround introduced in revision 0041.

Rollback note:
    ``DROP INDEX CONCURRENTLY IF EXISTS ix_facts_active_spo_expr`` then
    recreate ``ix_facts_active_spo`` (0041's partial index) — symmetric
    downgrade, no data touched.  The conflict scan falls back to a
    sequential scan until an index is restored.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-02
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_INDEX = "ix_facts_active_spo"
_NEW_INDEX = "ix_facts_active_spo_expr"


def upgrade() -> None:
    # ── 1. Leave Alembic's transaction — CONCURRENTLY is non-transactional ──
    op.execute("COMMIT")
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_OLD_INDEX}")
    op.execute("BEGIN")

    op.execute("COMMIT")
    op.execute(
        f"CREATE INDEX CONCURRENTLY {_NEW_INDEX} "
        "ON facts (project_id, lower(subject), lower(predicate), lower(object))"
    )
    op.execute("BEGIN")


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NEW_INDEX}")
    op.execute("BEGIN")

    op.execute("COMMIT")
    op.execute(
        f"CREATE INDEX CONCURRENTLY {_OLD_INDEX} "
        "ON facts (project_id, subject, predicate, object) "
        "WHERE invalid_at IS NULL"
    )
    op.execute("BEGIN")
