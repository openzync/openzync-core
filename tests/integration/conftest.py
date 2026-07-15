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

from typing import Any, AsyncGenerator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool


# ── Helpers ─────────────────────────────────────────────────────────────────


def asgi_transport(app: Any) -> ASGITransport:
    """Create an ASGITransport that injects ``scope["app"]``.

    ``httpx.ASGITransport`` does not set ``scope["app"]``, but the
    ``RateLimitMiddleware`` relies on it to access ``app.state.redis``.
    This wrapper injects the app reference so the middleware can find
    the Redis client.
    """
    async def _asgi_with_scope(
        scope: dict, receive: Any, send: Any,
    ) -> None:
        scope["app"] = app
        await app(scope, receive, send)

    return ASGITransport(app=_asgi_with_scope)

from core.db import get_async_session
from tests.conftest import (
    _start_postgres_container,
    _start_redis_container,
    _ensure_testcontainers_env,
)

# Module-level container registry.
# SQLAlchemy AsyncEngine uses __slots__ and rejects arbitrary attributes,
# so we store testcontainer references here instead.
_testcontainers: dict[str, object] = {}


@pytest_asyncio.fixture(scope="session")
async def engine():
    """Session-scoped async engine backed by a testcontainers PostgreSQL.

    Spins up a PostgreSQL 15 + pgvector container, applies Alembic
    migrations (via sync engine), and provides the async engine to all
    tests in the session.  The sync/async split is deliberate — Alembic
    operates in a pure synchronous context to avoid ``MissingGreenlet``
    errors.
    """
    _ensure_testcontainers_env()
    pg_container = _start_postgres_container()
    redis_container = _start_redis_container()
    _testcontainers["pg"] = pg_container
    _testcontainers["redis"] = redis_container

    # ── Step 1: Run Alembic migrations via a sync engine ─────────────────
    pg_url = pg_container.get_connection_url()
    # Strip the asyncpg driver suffix — Alembic runs in a sync context
    sync_url = pg_url.replace("+asyncpg", "")

    from sqlalchemy import create_engine as create_sync_engine

    sync_engine = create_sync_engine(sync_url, pool_pre_ping=True)

    from alembic.command import upgrade as alembic_upgrade
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig("alembic.ini")
    with sync_engine.connect() as sync_conn:
        alembic_cfg.attributes["connection"] = sync_conn
        alembic_upgrade(alembic_cfg, "head")
    sync_engine.dispose()

    # ── Step 2: Create the async engine for tests ────────────────────────
    driver_url = pg_url.replace("postgresql://", "postgresql+asyncpg://")
    async_engine = create_async_engine(
        driver_url,
        poolclass=NullPool,
        pool_pre_ping=True,
    )

    # ── Step 3: Seed bootstrap data ──────────────────────────────────────
    # Many integration tests assume a well-known organization UUID exists.
    from models.organization import Organization
    from sqlalchemy import text

    async with async_engine.connect() as conn:
        # Check if bootstrap org exists
        result = await conn.execute(
            text("SELECT 1 FROM organizations WHERE id = '00000000-0000-0000-0000-000000000001'")
        )
        if not result.scalar():
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name, plan) "
                    "VALUES ('00000000-0000-0000-0000-000000000001', 'Bootstrap Org', 'free')"
                )
            )
        # Seed a project for tests that need project-scoped entities
        result = await conn.execute(
            text(
                "SELECT 1 FROM projects WHERE id = '00000000-0000-0000-0000-000000000002'"
            )
        )
        if not result.scalar():
            await conn.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name) "
                    "VALUES ('00000000-0000-0000-0000-000000000002', "
                    "'00000000-0000-0000-0000-000000000001', 'Integration Test Project')"
                )
            )
        await conn.commit()

    yield async_engine

    # ── Teardown ─────────────────────────────────────────────────────────
    await async_engine.dispose()
    pg_container.stop()
    redis_container.stop()
    _testcontainers.clear()


@pytest_asyncio.fixture
async def redis_client(engine) -> Any:
    """Function-scoped async Redis client connected to testcontainers Redis.

    Function scope ensures the client is created on the same event loop as
    the test that uses it, preventing ``Future attached to a different loop``
    errors when the session-scoped ``app`` fixture uses ``loop_scope="function"``.

    The underlying Redis container is session-scoped (started by ``engine``),
    so only a lightweight connection is created/closed per test.
    """
    from redis.asyncio import Redis as AsyncRedis

    container = _testcontainers["redis"]
    redis_url = f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"

    client = AsyncRedis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
    )
    yield client
    try:
        await client.aclose()
    except RuntimeError:
        pass  # event loop already closed during fixture teardown


@pytest_asyncio.fixture(loop_scope="function")
async def app(engine, redis_client) -> Any:
    """Create the FastAPI app wired to the testcontainers database + Redis.

    Before importing ``create_app`` we must initialise the ``Settings``
    singleton, because ``create_app()`` calls ``get_settings()`` at
    construction time (module-level ``app = create_app()`` in main.py).
    """
    from core.config import Settings, set_settings

    pg_container = _testcontainers["pg"]
    pg_url = pg_container.get_connection_url()
    driver_url = pg_url.replace("postgresql://", "postgresql+asyncpg://")
    redis_container = _testcontainers["redis"]
    redis_url = (
        f"redis://{redis_container.get_container_host_ip()}:"
        f"{redis_container.get_exposed_port(6379)}/0"
    )
    settings = Settings(
        DATABASE_URL=driver_url,
        REDIS_URL=redis_url,
        SECRET_KEY="i" * 32,
        WEBHOOK_SIGNING_SECRET="j" * 32,
    )
    set_settings(settings)

    from services.api.main import create_app
    from dependencies.db import get_db

    app = create_app()
    session_factory = get_async_session(engine)
    app.state.db_session_factory = session_factory

    # Wire Redis client — the app's lifespan normally does this, but it
    # is not run when we call create_app() directly in tests.
    app.state.redis = redis_client

    # Wire a mock OpenBao client — the real lifespan tries to establish
    # a real connection, which we skip in integration tests.  Routes that
    # depend on openbao_client (e.g. org config) will use this mock.
    from unittest.mock import AsyncMock

    mock_bao = AsyncMock()
    mock_bao.read_org_config.return_value = {}
    mock_bao.write_org_config.return_value = None
    app.state.openbao_client = mock_bao

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
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def org_and_key(app: Any) -> dict:
    """Create a test org + API key via the admin bootstrap endpoint."""
    transport = asgi_transport(app)
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
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {org_and_key['api_key']}"
        yield client
