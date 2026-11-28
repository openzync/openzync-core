"""Integration test for migration 0082 — ``platform:manage`` backfill.

Runs against a REAL PostgreSQL (testcontainers, same infra as
``test_migration_0050.py``).  Applies the chain up to 0081, seeds
pre-0082 users (root with NULL permissions, admins, plain members),
applies 0082, and asserts the backfill contract:

- ``external_id = 'root'`` ends with exactly ``['platform:manage']``;
- admins (``role = 'admin'``) gain the grant (unioned, no duplicates),
  including from NULL;
- non-root/non-admin rows are untouched (no grant added);
- re-running the migration is a no-op (stable, no duplicate entries).

There is no 0083 — this file covers 0082 only.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import (
    _ensure_testcontainers_env,
    _start_postgres_container,
    sync_database_url,
)

pytestmark = pytest.mark.integration


def _run_alembic(sync_engine: Any, revision: str) -> None:
    """Run ``alembic upgrade/downgrade <revision>`` against the sync engine."""
    from alembic.command import downgrade as alembic_downgrade
    from alembic.command import upgrade as alembic_upgrade
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig("alembic.ini")
    with sync_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
        if revision.startswith("<"):
            alembic_downgrade(alembic_cfg, revision[1:])
        else:
            alembic_upgrade(alembic_cfg, revision)


@pytest.fixture(scope="module")
def pg() -> Any:
    """A module-scoped testcontainers PostgreSQL, torn down at module end."""
    _ensure_testcontainers_env()
    container = _start_postgres_container()
    yield container
    container.stop()


@pytest.fixture(scope="module")
def sync_engine(pg: Any) -> Any:
    """A sync engine to the testcontainers PG (Alembic runs synchronously)."""
    url = sync_database_url(pg.get_connection_url())
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


def _seed_pre_0082_users(engine: Any) -> None:
    """Insert root/admin/member rows as they looked before 0082."""
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.bypass_rls', 'true', false)"))
        conn.execute(
            text(
                "INSERT INTO organizations (id, name) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'Acme')"
            )
        )
        # root with NULL permissions; admin with NULL; admin with a grant;
        # plain member; member with a custom grant (must stay untouched).
        # Empty-array permissions stand in for NULL: the column is
        # NOT NULL at 0081, so true-NULL rows are unrepresentable — the
        # migration's COALESCE guard covers them by construction, and '{}'
        # exercises the same empty-input branch.
        conn.execute(
            text(
                "INSERT INTO users "
                "(id, external_id, role, permissions) VALUES "
                "('00000000-0000-0000-0000-000000000030', "
                "'root', 'member', '{}'), "
                "('00000000-0000-0000-0000-000000000031', "
                "'admin1', 'admin', '{}'), "
                "('00000000-0000-0000-0000-000000000032', "
                "'admin2', 'admin', "
                "'{project:read}'), "
                "('00000000-0000-0000-0000-000000000033', "
                "'member1', 'member', "
                "'{project:read,project:write}'), "
                "('00000000-0000-0000-0000-000000000034', "
                "'member2', 'member', "
                "'{project:read,members:read}')"
            )
        )
        conn.commit()


def _fetch_permissions(engine: Any, row_id: str) -> list[str] | None:
    """Read the raw ``permissions`` array for a user row (None stays None)."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT permissions FROM users WHERE id = :id"),
            {"id": row_id},
        ).fetchone()
        return list(row[0]) if row and row[0] is not None else None


class TestMigration0082:
    """Migration 0082 backfill — root + admins gain ``platform:manage``."""

    def test_backfill_root_admins_and_leaves_members(self, sync_engine: Any) -> None:
        """Apply 0081, seed, apply 0082, assert the backfill contract."""
        _run_alembic(sync_engine, "0081")
        _seed_pre_0082_users(sync_engine)
        _run_alembic(sync_engine, "0082")

        # root (NULL) ends with exactly the grant.
        assert (
            _fetch_permissions(sync_engine, "00000000-0000-0000-0000-000000000030")
            == ["platform:manage"]
        )
        # admin (NULL) gains the grant.
        assert (
            _fetch_permissions(sync_engine, "00000000-0000-0000-0000-000000000031")
            == ["platform:manage"]
        )
        # admin with an existing grant keeps it, unioned and sorted.
        assert (
            _fetch_permissions(sync_engine, "00000000-0000-0000-0000-000000000032")
            == ["platform:manage", "project:read"]
        )
        # non-root/non-admin rows are untouched.
        assert _fetch_permissions(
            sync_engine, "00000000-0000-0000-0000-000000000033"
        ) == ["project:read", "project:write"]
        assert sorted(
            _fetch_permissions(sync_engine, "00000000-0000-0000-0000-000000000034")
            or []
        ) == ["members:read", "project:read"]

    def test_backfill_is_idempotent(self, sync_engine: Any) -> None:
        """Running 0082 a second time changes nothing (no dupes, no clobber).

        The downgrade body is a deliberate no-op — stepping back to 0081
        only moves the version marker, so upgrading to 0082 again
        re-executes the backfill SQL for real.  Reuses the rows seeded by
        the previous test (module-scoped database).
        """
        _run_alembic(sync_engine, "<0081")
        _run_alembic(sync_engine, "0082")

        # root is stable at exactly the grant — no duplicate entries.
        assert (
            _fetch_permissions(sync_engine, "00000000-0000-0000-0000-000000000030")
            == ["platform:manage"]
        )
        # NULL-permission admin is safe; existing grants are preserved.
        assert (
            _fetch_permissions(sync_engine, "00000000-0000-0000-0000-000000000031")
            == ["platform:manage"]
        )
        assert (
            _fetch_permissions(sync_engine, "00000000-0000-0000-0000-000000000032")
            == ["platform:manage", "project:read"]
        )
        # non-root/non-admin rows still untouched after the re-run.
        assert _fetch_permissions(
            sync_engine, "00000000-0000-0000-0000-000000000033"
        ) == ["project:read", "project:write"]
        assert sorted(
            _fetch_permissions(sync_engine, "00000000-0000-0000-0000-000000000034")
            or []
        ) == ["members:read", "project:read"]
