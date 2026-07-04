"""Integration tests for Alembic migrations against a real PostgreSQL instance.

These tests use ``subprocess`` to invoke ``alembic`` CLI commands against the
database configured via ``DATABASE_URL`` (or ``OZ_DATABASE_URL``).

All tests are skipped by default because they require:
- A running PostgreSQL instance (use testcontainers or a local PG)
- The ``DATABASE_URL`` / ``OZ_DATABASE_URL`` environment variable to be set
"""

from __future__ import annotations

import os
import subprocess

import pytest

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "../.."))


@pytest.mark.skip(reason="Requires testcontainers + real PostgreSQL instance")
class TestMigrations:
    """Verify that Alembic migrations can apply and roll back cleanly."""

    def test_upgrade_head_creates_tables(self) -> None:
        """``alembic upgrade head`` should succeed without errors."""
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, (
            f"Migration failed:\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )

    def test_downgrade_base(self) -> None:
        """``alembic downgrade base`` should revert all migrations cleanly."""
        # Ensure we are at head first
        subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True,
            cwd=PROJECT_ROOT,
        )

        result = subprocess.run(
            ["alembic", "downgrade", "base"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, (
            f"Downgrade failed:\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )

    def test_migration_idempotent(self) -> None:
        """Re-applying ``upgrade head`` after a full downgrade must succeed.

        This validates that every migration's ``downgrade()`` correctly
        reverses its ``upgrade()`` so the cycle can be repeated.
        """
        # Start from base
        subprocess.run(
            ["alembic", "downgrade", "base"],
            capture_output=True,
            cwd=PROJECT_ROOT,
        )

        # Re-apply
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, (
            f"Idempotent upgrade failed:\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )

    def test_current_revision_matches_head(self) -> None:
        """``alembic current`` should report the same revision as ``heads``."""
        # Apply head
        subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True,
            cwd=PROJECT_ROOT,
        )

        # Get current revision
        current = subprocess.run(
            ["alembic", "current"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        # Get expected head revision
        heads = subprocess.run(
            ["alembic", "heads"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )

        assert current.returncode == 0
        assert heads.returncode == 0
        # Both should contain the same revision identifier
        assert current.stdout.strip(), "No current revision found — is the DB empty?"
        assert current.stdout.strip() == heads.stdout.strip(), (
            f"DB is at {current.stdout.strip()!r} but head is {heads.stdout.strip()!r}"
        )

    def test_history_is_linear(self) -> None:
        """The migration history should have exactly one head (linear lineage)."""
        heads = subprocess.run(
            ["alembic", "heads"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert heads.returncode == 0

        # One revision per line, minus trailing newline
        num_heads = len([line for line in heads.stdout.strip().split("\n") if line.strip()])
        assert num_heads == 1, (
            f"Expected exactly 1 head, found {num_heads}:\n{heads.stdout}"
        )
