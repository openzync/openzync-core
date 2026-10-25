"""Regression: embedding reset runs per-org tenant loop (item 7).

- ``dry_run`` writes nothing (no UPDATE, no commit).
- Writes null embeddings per org under ``set_tenant_context`` — no bulk
  rewrite ever runs under the global ``bypass_rls`` escape hatch.
- Empty org list is a no-op.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "reset_embeddings_for_remodel.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "reset_embeddings_for_remodel", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORG_A = UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B = UUID("00000000-0000-0000-0000-0000000000b2")


class _FakeSession:
    """Minimal async session: records statements, fakes counts."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN202
        self.statements.append(str(stmt))
        result = MagicMock()
        result.scalar_one.return_value = 3
        result.scalars.return_value.all.return_value = [ORG_A, ORG_B]
        return result

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _patch_db(module, sessions: list[_FakeSession], monkeypatch) -> dict:
    """Wire the module's DB surface to fake sessions; return call record."""
    calls: dict = {"bypass": [], "tenant": []}

    async def _bypass(db, value):  # noqa: ANN001, ANN202
        calls["bypass"].append(value)

    async def _tenant(db, org_id):  # noqa: ANN001, ANN202
        calls["tenant"].append(org_id)

    it = iter(sessions)

    def _factory(engine):  # noqa: ANN001, ANN202
        def _make() -> _FakeSession:
            return next(it)

        return _make

    monkeypatch.setattr(module, "init_db_engine", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(module, "get_async_session", _factory)
    monkeypatch.setattr(module, "set_bypass_rls", _bypass)
    monkeypatch.setattr(module, "set_tenant_context", _tenant)
    engine = module.init_db_engine.return_value
    engine.dispose = AsyncMock()
    return calls


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(monkeypatch) -> None:
    """Dry run only counts — no UPDATE statements, no commits."""
    module = _load_module()
    sessions = [_FakeSession(), _FakeSession(), _FakeSession()]
    _patch_db(module, sessions, monkeypatch)

    await module.run("postgresql+asyncpg://test/db", dry_run=True)

    writes = [s for sess in sessions for s in sess.statements if "UPDATE" in s.upper()]
    assert writes == []
    assert all(sess.commits == 0 for sess in sessions)


@pytest.mark.asyncio
async def test_write_loops_per_org_tenant(monkeypatch) -> None:
    """Writes run once per org under that org's tenant context."""
    module = _load_module()
    sessions = [_FakeSession() for _ in range(3)]  # list-orgs + 2 orgs
    calls = _patch_db(module, sessions, monkeypatch)

    await module.run("postgresql+asyncpg://test/db", dry_run=False)

    # Per-org tenant loop — both orgs bound, one commit each.
    assert calls["tenant"] == [ORG_A, ORG_B]
    write_sessions = sessions[1:]
    for sess in write_sessions:
        updates = [s for s in sess.statements if "UPDATE" in s.upper()]
        assert len(updates) == 2  # episodes + facts
        assert sess.commits == 1
    # Episodes + facts updates are org-scoped (no global wipe).
    assert any(
        "organization_id" in s for sess in write_sessions for s in sess.statements
    )
    # The global bypass is only the read-only org enumeration, never a write.
    assert calls["bypass"] == [True]


@pytest.mark.asyncio
async def test_no_orgs_is_noop(monkeypatch) -> None:
    """No organizations → nothing counted, committed, or looped."""
    module = _load_module()
    listing = _FakeSession()
    listing.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
    )
    calls = _patch_db(module, [listing], monkeypatch)

    await module.run("postgresql+asyncpg://test/db", dry_run=False)

    assert calls["tenant"] == []
    assert listing.commits == 0
