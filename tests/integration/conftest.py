"""Integration test fixtures — testcontainers-powered PostgreSQL and Redis.

Every integration test gets an isolated PostgreSQL + Redis stack via
``testcontainers``.  Alembic migrations are applied automatically before
the first test, and containers are torn down at session end.

Fixtures provided:
    - ``engine`` — session-scoped async SQLAlchemy engine connected to the
      testcontainers PostgreSQL.
    - ``redis_client`` — session-scoped async Redis client connected to
      the testcontainers Redis.
    - ``app`` — FastAPI application with the DB session factory overridden
      to point at the test PG.
    - ``async_client`` — HTTP test client (ASGITransport) backed by ``app``.
    - ``org_and_key`` — bootstraps a test org + API key.
    - ``auth_client`` — ``async_client`` pre-authenticated with the API key.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import settings
from core.db import get_async_session
from tests.conftest import (
    _start_postgres_container,
    _start_redis_container,
    _run_alembic_upgrade,
    _ensure_testcontainers_env,
)


@pytest_asyncio.fixture(scope="session")
async def engine():
    """Session-scoped async engine backed by a testcontainers PostgreSQL.

    Spins up a PostgreSQL 15 + pgvector container, applies Alembic
    migrations, and provides the engine to all tests in the session.
    """
    _ensure_testcontainers_env()
    container = _start_postgres_container()
    redis_container = _start_redis_container()

    # ── Wait for both to be ready ────────────────────────────────────────
    # testcontainers already blocks on ``container.start()`` until the
    # health check passes, so we can connect immediately.
    pg_url = container.get_connection_url()
    driver_url = pg_url.replace("postgresql://", "postgresql+asyncpg://")

    engine = create_async_engine(
        driver_url,
        poolclass=NullPool,
        pool_pre_ping=True,
    )

    # ── Apply Alembic migrations ─────────────────────────────────────────
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory

    alembic_cfg = AlembicConfig("alembic.ini")
    script = ScriptDirectory.from_config(alembic_cfg)

    async def _run_migrations() -> None:
        def do_upgrade(rev, context):
            return script._upgrade_revs("head", rev)

        async with engine.connect() as conn:
            await conn.run_sync(
                lambda sync_conn: EnvironmentContext(alembic_cfg, script).configure(
                    sync_conn,
                    fn=do_upgrade,
                )
            )

    try:
        await _run_migrations()
    except Exception as exc:
        # If migrations fail, try a simpler approach
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.commit()

        # Run raw DDL from the initial migration
        from alembic.command import upgrade as alembic_upgrade

        def _upgrade():
            alembic_cfg.attributes["connection"] = None
            with engine.sync_engine.connect() as sync_conn:
                alembic_cfg.attributes["connection"] = sync_conn
                alembic_upgrade(alembic_cfg, "head")

        await asyncio.get_event_loop().run_in_executor(None, _upgrade)

    # ── Store for teardown ───────────────────────────────────────────────
    engine._testcontainers_pg = container  # type: ignore[attr-defined]
    engine._testcontainers_redis = redis_container  # type: ignore[attr-defined]

    yield engine

    # ── Teardown ─────────────────────────────────────────────────────────
    await engine.dispose()
    container.stop()
    redis_container.stop()


@pytest_asyncio.fixture(scope="session")
async def redis_client(engine) -> Any:
    """Session-scoped async Redis client connected to testcontainers Redis."""
    from redis.asyncio import Redis as AsyncRedis

    container = engine._testcontainers_redis  # type: ignore[attr-defined]
    redis_url = f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"

    client = AsyncRedis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
    )
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def app(engine) -> Any:
    """Create the FastAPI app wired to the testcontainers database."""
    from services.api.main import create_app
    from dependencies.db import get_db

    app = create_app()
    session_factory = get_async_session(engine)
    app.state.db_session_factory = session_factory

    async def _get_db_override() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db_override
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def async_client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client backed by the FastAPI test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def org_and_key(app: Any) -> dict:
    """Create a test org + API key via the admin bootstrap endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/organizations",
            json={"name": "Test Org", "plan": "free"},
        )
        assert resp.status_code == 201, f"Admin bootstrap failed: {resp.text}"
        data = resp.json()
        return {
            "org_id": UUID(data["organization_id"]),
            "api_key": data["api_key"],
        }


@pytest_asyncio.fixture
async def auth_client(app: Any, org_and_key: dict) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client pre-authenticated with a real API key."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {org_and_key['api_key']}"
        yield client
