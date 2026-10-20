"""Shared fixtures for worker-task unit tests.

The episode workers (compute_observations, embed_episode, enrich_episode,
extract_blob_text) each open a fail-closed archived-project guard —
``ProjectRepository(db).is_archived(...)`` — *before* any real work.  Against
a mocked session the real query returns a truthy mock, so every task
early-returns "project_archived_skipping" and no assertion past the guard is
ever reached.

Default this directory's projects to **not archived**.  A test that needs the
archived path patches ``ProjectRepository`` again inside its own ``with``
block, which takes precedence over this fixture.

The reconcile crons additionally read their target queue from the worker's
own ``WorkerSettings`` singleton — see :func:`_seed_worker_settings`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _project_not_archived() -> Iterator[None]:
    """Make the archived-project guard non-archived for every task test."""
    with patch("repositories.project_repository.ProjectRepository") as project_cls:
        project_cls.return_value.is_archived = AsyncMock(return_value=False)
        yield


@pytest.fixture(autouse=True)
def _seed_worker_settings() -> Iterator[None]:
    """Seed the ``WorkerSettings`` singleton for every test in this directory.

    ``workers/tasks/reconcile_enrichment.py`` and
    ``workers/tasks/reconcile_graph_edges.py`` read their target queue from
    ``services.worker.worker_settings.settings`` — a proxy that raises
    ``RuntimeError`` until the module-level singleton is populated (normally
    by ``init_worker_settings_from_bao()`` at worker startup).

    Without this fixture the reconcile tests only passed when some *other*
    test file happened to seed the singleton earlier in the session — an
    order-dependent pass that made the directory fail 14/14 in isolation.
    The previous value is restored on teardown so this directory neither
    depends on nor leaks global state into the rest of the suite.

    ``ENV="test"`` makes the derived queue names
    ``OpenZync:test:queue:{high,low}``.
    """
    from services.worker import worker_settings as _ws
    from services.worker.worker_settings import WorkerSettings

    previous = _ws._settings
    _ws._settings = WorkerSettings(
        DATABASE_URL="postgresql+asyncpg://localhost:5432/test",
        REDIS_URL="redis://localhost:6379/0",
        ENV="test",
    )
    try:
        yield
    finally:
        _ws._settings = previous
