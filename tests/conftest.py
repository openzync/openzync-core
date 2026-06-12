"""Root test configuration — shared fixtures for all test levels.

Fixtures requiring the application stack (``app``, ``async_client``, etc.)
live in ``tests/integration/conftest.py`` to avoid import-time failures when
the application hasn't been built yet (unit tests).

Testcontainers helpers live here so they can be shared between
``tests/integration/conftest.py`` and ``tests/security/conftest.py``.
"""

from __future__ import annotations

import os

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Testcontainers helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _ensure_testcontainers_env() -> None:
    """Set environment variables required by testcontainers.

    Disables Ryuk (resource reaper) in CI since the container runtime
    may not support it.  Also disables Docker host checks.
    """
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    os.environ.setdefault("TC_HOST", "localhost")


def _start_postgres_container() -> object:
    """Start a PostgreSQL 15 + pgvector testcontainer.

    Returns:
        The started container instance.  Connection URL is available via
        ``container.get_connection_url()``.
    """
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        image="pgvector/pgvector:pg15",
        user="memgraph",
        password="memgraph",
        dbname="memgraph_test",
    )
    container.start()
    return container


def _start_redis_container() -> object:
    """Start a Redis 7 testcontainer.

    Returns:
        The started container instance.  Host and port are available
        via ``container.get_container_host_ip()`` and
        ``container.get_exposed_port(6379)``.
    """
    from testcontainers.redis import RedisContainer

    container = RedisContainer(image="redis:7-alpine")
    container.start()
    return container


def _run_alembic_upgrade(driver_url: str) -> None:
    """Run Alembic migrations up to ``head`` against the given database.

    Args:
        driver_url: Full asyncpg connection URL for the database.
    """
    import asyncio

    from alembic.command import upgrade as alembic_upgrade
    from alembic.config import Config as AlembicConfig
    from sqlalchemy import create_engine

    # Alembic needs a sync engine for its migration runner
    sync_url = driver_url.replace("postgresql+asyncpg://", "postgresql://")
    sync_engine = create_engine(sync_url, pool_pre_ping=True)

    try:
        alembic_cfg = AlembicConfig("alembic.ini")
        alembic_cfg.attributes["connection"] = sync_engine.connect()
        alembic_upgrade(alembic_cfg, "head")
    finally:
        sync_engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# Auth helpers
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def test_api_key() -> str:
    """Return a synthetic API key for use in auth tests."""
    return "mg_test_" + "a" * 64
