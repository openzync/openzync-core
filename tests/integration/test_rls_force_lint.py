"""FORCE RLS lint rejects non-FORCE DDL (static migration scan, no I/O)."""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.integration


def test_migrations_use_force_rls() -> None:
    """Every ENABLE ROW LEVEL SECURITY in migrations must be FORCE."""
    root = pathlib.Path("alembic/versions")
    offenders: list[str] = []
    for f in root.glob("*.py"):
        text = f.read_text()
        for m in re.finditer(r"ENABLE ROW LEVEL SECURITY", text):
            window = text[max(0, m.start() - 200): m.end() + 50]
            if "FORCE" not in window.upper():
                offenders.append(f"{f.name}: {window.strip()[:120]}")
    assert not offenders, f"non-FORCE RLS DDL found: {offenders[:5]}"


def test_force_rls_script_exists() -> None:
    assert pathlib.Path("scripts/check_force_rls.py").exists()
