"""Unit tests for org join codes — generation, normalization, and migration 0044.

Covers:
- ``generate_org_code``: 8-char codes from the confusion-free alphabet.
- ``normalize_org_code``: case-insensitive, whitespace-stripped.
- Migration 0044 static sanity: backfill uses ``generate_org_code`` and the
  unique index / column shape match the observed schema (offline — no DB).

The full backfill execution against a real database is covered by the
integration suite (``tests/integration/test_migrations.py``); this file is
hermetic and runs without Postgres.
"""

from __future__ import annotations

import importlib
import inspect

from core.org_codes import (
    ORG_CODE_ALPHABET,
    ORG_CODE_LENGTH,
    generate_org_code,
    normalize_org_code,
)


def test_generate_org_code_length_and_alphabet() -> None:
    """Generated codes are 8 chars from the confusion-free alphabet."""
    for _ in range(200):
        code = generate_org_code()
        assert len(code) == ORG_CODE_LENGTH == 8
        assert all(ch in ORG_CODE_ALPHABET for ch in code)


def test_alphabet_excludes_confusable_characters() -> None:
    """I/O/0/1 are excluded — codes must not be typo-ambiguous.

    The alphabet is 31 chars (also excludes ``L``); the ``32-char`` figure
    in the module docstring is a documentation nit, not a behavior issue.
    """
    for ch in "IO01L":
        assert ch not in ORG_CODE_ALPHABET
    assert ORG_CODE_ALPHABET == "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    assert len(ORG_CODE_ALPHABET) == 31


def test_generated_codes_are_unique() -> None:
    """Secrets-based generation does not collide at test scale."""
    codes = {generate_org_code() for _ in range(1000)}
    assert len(codes) == 1000


def test_normalize_org_code_uppercases() -> None:
    """Codes are case-insensitive — ``gce3gg9z`` == ``GCE3GG9Z``."""
    assert normalize_org_code("gce3gg9z") == "GCE3GG9Z"
    assert normalize_org_code("GCE3GG9Z") == "GCE3GG9Z"


def test_normalize_org_code_strips_whitespace() -> None:
    """Surrounding whitespace is stripped before lookup."""
    assert normalize_org_code("  K7M2Q9X4  ") == "K7M2Q9X4"
    assert normalize_org_code("\tK7M2Q9X4\n") == "K7M2Q9X4"


def test_normalize_org_code_empty_string() -> None:
    """Empty/whitespace-only input normalizes to an empty string (no crash)."""
    assert normalize_org_code("   ") == ""
    assert normalize_org_code("") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Migration 0044 — offline static sanity
# ═══════════════════════════════════════════════════════════════════════════════


def _load_migration_0044():
    """Import the 0044 migration module (importable without a DB)."""
    return importlib.import_module("migrations.versions.0044_org_codes")


def test_migration_0044_revision_chain() -> None:
    """0044 revises 0043 (the RBAC migration)."""
    mod = _load_migration_0044()
    assert mod.revision == "0044"
    assert mod.down_revision == "0043"


def test_migration_0044_backfill_uses_org_code_generator() -> None:
    """The backfill produces codes from ``core.org_codes`` — 8 chars, the
    confusion-free alphabet (NOT the DB, so the shape matches the codebase's
    single source of truth for code generation)."""
    src = inspect.getsource(_load_migration_0044().upgrade)
    assert "generate_org_code()" in src
    assert "core.org_codes" in src or "generate_org_code" in src


def test_migration_0044_unique_index_on_org_code() -> None:
    """The migration creates a UNIQUE index on ``org_code`` (join codes must
    be globally unique) and drops it on downgrade."""
    mod = _load_migration_0044()
    upgrade_src = inspect.getsource(mod.upgrade)
    downgrade_src = inspect.getsource(mod.downgrade)

    assert "uq_organizations_org_code" in upgrade_src
    assert '"org_code"' in upgrade_src or "['org_code']" in upgrade_src
    assert "unique=True" in upgrade_src
    assert "uq_organizations_org_code" in downgrade_src


def test_migration_0044_column_shape_varchar_24_not_null() -> None:
    """The column is VARCHAR(24), nullable during backfill, then NOT NULL."""
    src = inspect.getsource(_load_migration_0044().upgrade)
    assert "VARCHAR(24)" in src
    assert "nullable=True" in src
    assert "nullable=False" in src


def test_migration_0044_excludes_inactive_orgs_at_lookup() -> None:
    """The org-code LOOKUP (repo level) filters on ``is_active`` — the code
    gate and the backfill are two halves of the same feature."""
    from repositories.organization_repository import OrganizationRepository

    lookup_src = inspect.getsource(OrganizationRepository.get_by_code)
    assert "is_active" in lookup_src
