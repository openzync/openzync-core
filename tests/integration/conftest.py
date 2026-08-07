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

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

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
    _ensure_testcontainers_env,
    _start_postgres_container,
    _start_redis_container,
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

    from dependencies.db import get_db
    from services.api.main import create_app

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


# ═══════════════════════════════════════════════════════════════════════════════
# Per-test DB isolation fixtures
# ═══════════════════════════════════════════════════════════════════════════════
#
# These fixtures provide **full per-test database isolation** via a
# connection-level transaction that is rolled back when the test ends.
# Every write during the test (org creation, user creation, etc.) goes
# into the same uncommitted transaction — zero state leakage between tests.
#
# Usage in test files:
#
#     class TestFoo:
#         @pytest.fixture
#         async def auth_client(self, isolated_app):
#             transport = asgi_transport(isolated_app)
#             async with AsyncClient(...) as c:
#                 resp = await c.post("/admin/organizations", ...)
#                 c.headers["Authorization"] = f"Bearer {resp.json()['api_key']}"
#                 yield c
#
#         @pytest.mark.asyncio
#         async def test_something(self, auth_client):
#             ...


@pytest_asyncio.fixture(loop_scope="function")
async def db_session(engine) -> AsyncGenerator[None, None]:
    """Per-test DB isolation via table truncation on teardown.

    Session-per-request (like ``app`` fixture) but truncates every
    table at the end so no state leaks between tests.  Truncation uses
    ``CASCADE`` so foreign-key order does not matter.

    This approach is used instead of connection-level rollback because
    the ``AuthMiddleware`` opens its own session via
    ``app.state.db_session_factory`` and must be able to see rows
    created during the test (e.g. API keys).  A shared uncommitted
    transaction would be invisible to those sessions.
    """
    yield
    # ── Teardown: flush all state ───────────────────────────────────────
    # 1. Truncate all DB tables (via ORM metadata).
    # 2. Flush Redis (rate-limit counters, auth-miss counters, caches).
    from sqlalchemy import text as _sql

    import models  # noqa: F401 — register models on Base.metadata
    from models.base import Base

    async with engine.connect() as conn:
        await conn.execute(_sql("SET session_replication_role = 'replica'"))
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(_sql(f"TRUNCATE TABLE {table.fullname} CASCADE"))
        await conn.execute(_sql("SET session_replication_role = 'origin'"))

        # Re-insert session-scoped seed data (these well-known UUIDs are
        # created by the session-scoped engine fixture and relied on by
        # many integration tests, e.g. test_user_repository.py).
        for stmt in (
            "INSERT INTO organizations (id, name, plan) "
            "VALUES ('00000000-0000-0000-0000-000000000001', "
            "'Bootstrap Org', 'free') ON CONFLICT (id) DO NOTHING",
            "INSERT INTO projects (id, organization_id, name) "
            "VALUES ('00000000-0000-0000-0000-000000000002', "
            "'00000000-0000-0000-0000-000000000001', "
            "'Integration Test Project') ON CONFLICT (id) DO NOTHING",
        ):
            await conn.execute(_sql(stmt))
        await conn.commit()

    from redis.asyncio import Redis as AsyncRedis

    container = _testcontainers["redis"]
    redis_url = (
        f"redis://{container.get_container_host_ip()}:"
        f"{container.get_exposed_port(6379)}/0"
    )
    flush_client = AsyncRedis.from_url(redis_url, socket_connect_timeout=3)
    try:
        await flush_client.flushall()
    finally:
        await flush_client.aclose()


@pytest_asyncio.fixture(loop_scope="function")
async def isolated_app(engine, redis_client, db_session) -> Any:
    """FastAPI app with per-test DB isolation via table-truncation cleanup.

    Identical to :func:`app` in behaviour — one session per request,
    auto-commit on success — but the ``db_session`` fixture truncates
    all tables on teardown, ensuring zero state leakage between tests.
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

    test_app = create_app()
    from core.db import get_async_session

    session_factory = get_async_session(engine)
    test_app.state.db_session_factory = session_factory
    test_app.state.redis = redis_client

    from unittest.mock import AsyncMock

    mock_bao = AsyncMock()
    mock_bao.read_org_config.return_value = {}
    mock_bao.write_org_config.return_value = None
    test_app.state.openbao_client = mock_bao

    # Init ARQ (async Redis queue) so memory-ingestion tests can enqueue
    # enrichment jobs without hitting "ARQ pool not initialised".
    from core.arq import close_arq, init_arq

    arq_pool = await init_arq(str(settings.REDIS_URL))
    test_app.state.arq_pool = arq_pool

    async def _get_db_override() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    test_app.dependency_overrides[get_db] = _get_db_override
    yield test_app
    test_app.dependency_overrides.clear()
    await close_arq()


@pytest_asyncio.fixture(loop_scope="function")
async def isolated_org_and_key(isolated_app: Any) -> dict:
    """Bootstrap a test org + API key — cleaned up by table truncation.

    Endpoints that depend on ``get_current_user_id`` require a valid user
    UUID in ``request.state.user_id``.  API keys created via the admin
    bootstrap don't have a ``created_by`` user, so this fixture also:
    1. Creates a test user via the Users API.
    2. Sets a ``dependency_overrides`` for ``get_current_user_id`` on the
       app so it returns the test user's UUID.
    """
    from dependencies.auth import get_current_user_id

    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/organizations",
            json={"name": "Test Org", "plan": "free"},
        )
        assert resp.status_code == 201, f"Admin bootstrap failed: {resp.text}"
        data = resp.json()
        org_id = UUID(data["organization_id"])
        api_key = data["api_key"]

        # Look up the default project.
        client.headers["Authorization"] = f"Bearer {api_key}"
        proj_resp = await client.get("/v1/projects")
        assert proj_resp.status_code == 200, (
            f"Project lookup failed: {proj_resp.text}"
        )
        projects: list = proj_resp.json()
        assert isinstance(projects, list) and len(projects) >= 1
        project_id = UUID(projects[0]["id"])

        # Create a test user so get_current_user_id has something to return.
        user_resp = await client.post(
            "/v1/users",
            json={"external_id": "fixture_user"},
        )
        assert user_resp.status_code == 201
        user_id = UUID(user_resp.json()["id"])

        # Override get_current_user_id to return this user's UUID.
        isolated_app.dependency_overrides[get_current_user_id] = lambda: user_id

        return {
            "org_id": org_id,
            "api_key": api_key,
            "project_id": project_id,
            "user_id": user_id,
        }


@pytest_asyncio.fixture(loop_scope="function")
async def isolated_auth_client(
    isolated_app: Any,
    isolated_org_and_key: dict,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client pre-authenticated, backed by the isolated app."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = (
            f"Bearer {isolated_org_and_key['api_key']}"
        )
        yield client


@pytest_asyncio.fixture(loop_scope="function")
def isolated_project_id(isolated_org_and_key: dict) -> UUID:
    """Project ID from the bootstrap org — for constructing project-scoped URLs.

    Usage in tests::

        async def test_something(self, isolated_auth_client, isolated_project_id):
            resp = await isolated_auth_client.post(
                f"/v1/projects/{isolated_project_id}/sessions",
                ...
            )
    """
    return isolated_org_and_key["project_id"]
