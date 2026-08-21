"""Integration test for migration 0050 — unified permission-based RBAC.

Runs against a REAL PostgreSQL (testcontainers, same infra as
``tests/integration/conftest.py``).  It applies the migration chain up to
0049, seeds pre-0050 data (member/admin users, legacy-scoped API keys),
applies 0050, and asserts the backfill contract:

- members get the member defaults (``project:read``, ``project:write``);
- admins stay ``[]`` (wildcard via role);
- legacy API-key scopes map (``read`` → ``project:read``, ``write`` →
  ``project:write``, ``admin`` → all 7, ``admin:write`` → the trio);
- unknown legacy scopes are dropped (fail-safe);
- re-running the backfill is idempotent (custom grants are never clobbered).

This mirrors the existing ``test_migrations.py`` pattern (Alembic against a
real PG) but uses the testcontainers infra so it runs in CI without a
pre-provisioned database.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import (
    _ensure_testcontainers_env,
    _start_postgres_container,
)

pytestmark = pytest.mark.integration

ALL_PERMISSIONS = [
    "project:read",
    "project:write",
    "project:manage",
    "configuration:read",
    "configuration:write",
    "members:read",
    "members:write",
]
ADMIN_WRITE_TRIO = ["configuration:write", "members:write", "project:manage"]


def _run_alembic(sync_engine: Any, revision: str) -> None:
    """Run ``alembic upgrade <revision>`` against the given sync engine."""
    from alembic.command import upgrade as alembic_upgrade
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig("alembic.ini")
    with sync_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
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
    url = pg.get_connection_url().replace("+asyncpg", "")
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


def _seed_legacy_data(engine: Any) -> None:
    """Insert pre-0050 rows: member/admin users + legacy-scoped API keys.

    Uses ``app.bypass_rls`` so the DML is not filtered by the RLS policies
    (same as the migration itself).
    """
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.bypass_rls', 'true', false)"))
        conn.execute(
            text(
                "INSERT INTO organizations (id, name, plan, org_code) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'Acme', 'free', 'acme01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO projects (id, organization_id, name) VALUES "
                "('00000000-0000-0000-0000-000000000002', "
                "'00000000-0000-0000-0000-000000000001', 'Proj')"
            )
        )
        # Users: one member, one admin.
        conn.execute(
            text(
                "INSERT INTO users (id, organization_id, external_id, role) VALUES "
                "('00000000-0000-0000-0000-000000000010', "
                "'00000000-0000-0000-0000-000000000001', 'member1', 'member'), "
                "('00000000-0000-0000-0000-000000000011', "
                "'00000000-0000-0000-0000-000000000001', 'admin1', 'admin')"
            )
        )
        # API keys with legacy scopes.
        conn.execute(
            text(
                "INSERT INTO api_keys "
                "(id, organization_id, project_id, lookup_hash, key_hash, salt, "
                "prefix, scopes) VALUES "
                "('00000000-0000-0000-0000-000000000020', "
                "'00000000-0000-0000-0000-000000000001', "
                "'00000000-0000-0000-0000-000000000002', 'h1', 'k1', 's1', "
                "'oz_test_', '{read,write}'), "
                "('00000000-0000-0000-0000-000000000021', "
                "'00000000-0000-0000-0000-000000000001', "
                "'00000000-0000-0000-0000-000000000002', 'h2', 'k2', 's2', "
                "'oz_test_', '{admin}'), "
                "('00000000-0000-0000-0000-000000000022', "
                "'00000000-0000-0000-0000-000000000001', "
                "'00000000-0000-0000-0000-000000000002', 'h3', 'k3', 's3', "
                "'oz_test_', '{admin:write}'), "
                "('00000000-0000-0000-0000-000000000023', "
                "'00000000-0000-0000-0000-000000000001', "
                "'00000000-0000-0000-0000-000000000002', 'h4', 'k4', 's4', "
                "'oz_test_', '{read,unknown_scope}')"
            )
        )
        conn.commit()


def _fetch_permissions(engine: Any, table: str, row_id: str) -> list[str]:
    """Read the ``permissions`` array for a row."""
    with engine.connect() as conn:
        result = conn.execute(
            text(f"SELECT permissions FROM {table} WHERE id = :id"),
            {"id": row_id},
        )
        row = result.fetchone()
        return list(row[0]) if row and row[0] else []


class TestMigration0050:
    """Migration 0050 backfill — members, admins, and legacy API-key scopes."""

    def test_backfill_members_admins_and_legacy_scopes(
        self, sync_engine: Any,
    ) -> None:
        """Apply 0049, seed legacy data, apply 0050, assert the backfill."""
        _run_alembic(sync_engine, "0049")
        _seed_legacy_data(sync_engine)
        _run_alembic(sync_engine, "0050")

        # Members get the member defaults.
        assert _fetch_permissions(
            sync_engine, "users", "00000000-0000-0000-0000-000000000010"
        ) == ["project:read", "project:write"]
        # Admins stay [] (wildcard via role).
        assert _fetch_permissions(
            sync_engine, "users", "00000000-0000-0000-0000-000000000011"
        ) == []

        # Legacy API-key scopes map to the new vocabulary.
        assert _fetch_permissions(
            sync_engine, "api_keys", "00000000-0000-0000-0000-000000000020"
        ) == ["project:read", "project:write"]
        assert _fetch_permissions(
            sync_engine, "api_keys", "00000000-0000-0000-0000-000000000021"
        ) == sorted(ALL_PERMISSIONS)
        assert _fetch_permissions(
            sync_engine, "api_keys", "00000000-0000-0000-0000-000000000022"
        ) == sorted(ADMIN_WRITE_TRIO)
        # Unknown legacy scopes are dropped (fail-safe).
        assert _fetch_permissions(
            sync_engine, "api_keys", "00000000-0000-0000-0000-000000000023"
        ) == ["project:read"]

    def test_backfill_is_idempotent(self, sync_engine: Any) -> None:
        """Re-running the 0050 backfill does not clobber custom grants.

        The ``permissions = '{}'`` guard on the users UPDATE and the
        identity rows in the API-key scope map make a re-run a no-op.
        """
        # Give the member a custom grant, then re-apply 0050.
        with sync_engine.connect() as conn:
            conn.execute(
                text("SELECT set_config('app.bypass_rls', 'true', false)")
            )
            conn.execute(
                text(
                    "UPDATE users SET permissions = "
                    "'{project:read,project:write,configuration:read}' "
                    "WHERE id = '00000000-0000-0000-0000-000000000010'"
                )
            )
            conn.commit()

        _run_alembic(sync_engine, "0050")

        # The custom grant survives the re-run.
        assert set(_fetch_permissions(
            sync_engine, "users", "00000000-0000-0000-0000-000000000010"
        )) == {"configuration:read", "project:read", "project:write"}
        # The API-key backfill is a no-op (identity rows map values to
        # themselves).
        assert _fetch_permissions(
            sync_engine, "api_keys", "00000000-0000-0000-0000-000000000020"
        ) == ["project:read", "project:write"]