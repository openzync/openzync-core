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

from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.db import _register_pgvector_codec, get_async_session
from tests.conftest import (
    _ensure_testcontainers_env,
    _start_falkordb_container,
    _start_postgres_container,
    _start_redis_container,
    sync_database_url,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# ── Helpers ─────────────────────────────────────────────────────────────────


def asgi_transport(app: Any) -> ASGITransport:
    """Create an ASGITransport that injects ``scope["app"]``.

    ``httpx.ASGITransport`` does not set ``scope["app"]``, but the
    ``RateLimitMiddleware`` relies on it to access ``app.state.redis``.
    This wrapper injects the app reference so the middleware can find
    the Redis client.
    """

    async def _asgi_with_scope(
        scope: dict,
        receive: Any,
        send: Any,
    ) -> None:
        scope["app"] = app
        await app(scope, receive, send)

    return ASGITransport(app=_asgi_with_scope)


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
    falkordb_container = _start_falkordb_container()
    _testcontainers["pg"] = pg_container
    _testcontainers["redis"] = redis_container
    _testcontainers["falkordb"] = falkordb_container

    # ── Step 1: Run Alembic migrations via a sync engine ─────────────────
    pg_url = pg_container.get_connection_url()
    # Alembic runs in a sync context — pin the sync driver explicitly
    sync_url = sync_database_url(pg_url)

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
    # This test engine bypasses ``core.db.init_db_engine``, so it also bypasses
    # the prod factory's ``_register_pgvector_codec`` call (core/db.py:87).
    # Without the codec, asyncpg cannot decode the native ``VECTOR(768)`` OID
    # and every read/write touching ``episodes.embedding`` / ``facts.embedding``
    # raises ``DataError: expected str, got list``. Reuse the prod helper
    # verbatim — the private-underscore import is intentional; do not
    # "clean it up" or inline a local copy.
    _register_pgvector_codec(async_engine)

    # ── Step 3: Seed bootstrap data ──────────────────────────────────────
    # Many integration tests assume a well-known organization UUID exists.
    from sqlalchemy import text

    async with async_engine.connect() as conn:
        # Check if bootstrap org exists
        result = await conn.execute(
            text(
                "SELECT 1 FROM organizations "
                "WHERE id = '00000000-0000-0000-0000-000000000001'"
            )
        )
        if not result.scalar():
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name) "
                    "VALUES ('00000000-0000-0000-0000-000000000001', "
                    "'Bootstrap Org')"
                )
            )
        # Seed a project for tests that need project-scoped entities
        result = await conn.execute(
            text(
                "SELECT 1 FROM projects "
                "WHERE id = '00000000-0000-0000-0000-000000000002'"
            )
        )
        if not result.scalar():
            await conn.execute(
                text(
                    "INSERT INTO users (id, external_id, name, role) "
                    "VALUES ('00000000-0000-0000-0000-000000000001', "
                    "'bootstrap-user', 'Bootstrap User', 'admin') "
                    "ON CONFLICT (id) DO NOTHING"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO projects (id, user_id, name) "
                    "VALUES ('00000000-0000-0000-0000-000000000002', "
                    "'00000000-0000-0000-0000-000000000001', "
                    "'Integration Test Project')"
                )
            )
        await conn.commit()

    yield async_engine

    # ── Teardown ─────────────────────────────────────────────────────────
    await async_engine.dispose()
    pg_container.stop()
    redis_container.stop()
    falkordb_container.stop()
    _testcontainers.clear()


@pytest_asyncio.fixture(scope="session")
async def falkordb_url(engine) -> str:
    """Connection URL for the session-scoped FalkorDB testcontainer.

    FalkorDB speaks the Redis protocol — plain ``redis://`` URL, e.g.
    for per-org ``falkordb_url`` overrides in graph-backend tests.
    Depends on :func:`engine` so the container is started first.
    """
    container = _testcontainers["falkordb"]
    return (
        f"redis://{container.get_container_host_ip()}:"
        f"{container.get_exposed_port(6379)}"
    )


@pytest_asyncio.fixture(loop_scope="function")
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
    with suppress(RuntimeError):
        # Event loop already closed during fixture teardown
        await client.aclose()


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
        S3_ENDPOINT_URL="http://minio:9000",
        S3_ACCESS_KEY_ID="test-key",
        S3_SECRET_ACCESS_KEY="test-secret",
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


@pytest_asyncio.fixture(loop_scope="function")
async def async_client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client backed by the FastAPI test app."""
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def bootstrap_tenant(app: Any, client: AsyncClient, org_name: str) -> dict:
    """Bootstrap a full tenant under the current API contract.

    The org bootstrap (``POST /admin/organizations``) is now gated behind
    the platform ``admin`` role, so it is no longer usable as a
    public tenant-bootstrap path.  A working tenant is therefore created
    directly through the DB (test infra — there is no public "create org"
    endpoint that runs before auth exists):

    1. Insert the org row directly (fixture infra).
    2. Insert a member user row directly (fixture infra — there is no
       public "create user" endpoint that runs before auth exists).  The
       member carries the default grant set (``MEMBER_DEFAULT_PERMISSIONS``:
       everything except ``members:*``), so project/API-key bootstrap below
       still passes the ``project:manage`` gates while ``/v1/users*`` and
       audit-log reads stay 403 (platform-admin-only).
    3. Mint a JWT for that member (signed with the test app's SECRET_KEY).
    4. Create a project via ``POST /v1/projects`` with the JWT (the
       creator is auto-added as owner).
    5. Create a project-scoped API key via the JWT (owner-gated).

    Returns:
        Dict with ``user_id``, ``user_id``, ``jwt``, ``project_id`` and
        ``api_key`` — all created through the real API/middleware.
    """
    from datetime import timedelta
    from uuid import uuid4

    # ── Insert the org row directly (test infra, not a public API) ────
    from sqlalchemy import text as _sql

    from core.config import get_settings
    from core.rbac import MEMBER_DEFAULT_PERMISSIONS
    from repositories.user_repository import UserRepository
    from utils.crypto import create_jwt_token

    async with (
        app.state.db_session_factory() as session,
        session.begin(),
    ):
        user_id = uuid4()
        await session.execute(
            _sql(
                "INSERT INTO organizations (id, name) "
                "VALUES (:id, :name)"
            ),
            {"id": user_id, "name": org_name},
        )

    # ── Insert the user row directly (test infra, not a public API) ───
    async with (
        app.state.db_session_factory() as session,
        session.begin(),
    ):
        user = await UserRepository(session).create(
            external_id=f"bootstrap_{uuid4().hex[:8]}",
            role="member",
            permissions=list(MEMBER_DEFAULT_PERMISSIONS),
        )
    user_id = user.id

    # ── Mint a JWT for the member ─────────────────────────────────────
    jwt_token = create_jwt_token(
        data={
            "sub": str(user_id),
            "user_id": str(user_id),
            "role": "member",
            "type": "access",
        },
        secret=get_settings().SECRET_KEY,
        expires_delta=timedelta(hours=1),
    )
    client.headers["Authorization"] = f"Bearer {jwt_token}"

    # ── Create a project (JWT path auto-adds creator as owner) ──────────
    proj_resp = await client.post(
        "/v1/projects",
        json={"name": f"Project {uuid4().hex[:8]}"},
    )
    assert proj_resp.status_code == 201, f"Project creation failed: {proj_resp.text}"
    project_id = UUID(proj_resp.json()["id"])

    # ── Create a project-scoped API key (owner-gated, JWT only) ─────────
    # Project keys carry only project:* vocabulary — the service drops
    # anything else before persistence.
    key_resp = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={
            "name": "test-key",
            "permissions": [
                "project:read",
                "project:write",
            ],
        },
    )
    assert key_resp.status_code == 201, f"API key creation failed: {key_resp.text}"
    api_key = key_resp.json()["raw_key"]

    client.headers.pop("Authorization")
    return {
        "user_id": user_id,
        "user_id": user_id,
        "jwt": jwt_token,
        "project_id": project_id,
        "api_key": api_key,
    }


@pytest_asyncio.fixture(loop_scope="function")
async def org_and_key(app: Any) -> dict:
    """Create a test org + project + API key via the real API."""
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await bootstrap_tenant(app, client, "Test Org")


@pytest_asyncio.fixture(loop_scope="function")
async def auth_client(app: Any, org_and_key: dict) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client pre-authenticated with a real API key."""
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {org_and_key['api_key']}"
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def admin_client(
    app: Any, org_and_key: dict
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client pre-authenticated with the bootstrap member JWT.

    Project-scoped endpoints (projects, sessions, API keys, search,
    schemas, platform-config) pass on the member default grant set.
    Org-wide user management (``POST/GET/PATCH/DELETE /v1/users*``) and
    audit-log reads are platform-admin-gated — use
    ``isolated_superadmin_client`` (or ``superadmin_jwt``) for those.
    """
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {org_and_key['jwt']}"
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
#                 tenant = await bootstrap_tenant(isolated_app, c, "Test Org")
#                 c.headers["Authorization"] = f"Bearer {tenant['api_key']}"
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
            "INSERT INTO users (id, external_id, name, role) "
            "VALUES ('00000000-0000-0000-0000-000000000001', "
            "'bootstrap-user', 'Bootstrap User', 'admin') ON CONFLICT (id) DO NOTHING",
            "INSERT INTO organizations (id, name) "
            "VALUES ('00000000-0000-0000-0000-000000000001', "
            "'Bootstrap Org') ON CONFLICT (id) DO NOTHING",
            "INSERT INTO projects (id, user_id, name) "
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
        S3_ENDPOINT_URL="http://minio:9000",
        S3_ACCESS_KEY_ID="test-key",
        S3_SECRET_ACCESS_KEY="test-secret",
    )
    set_settings(settings)

    from dependencies.db import get_db
    from services.api.main import create_app

    test_app = create_app()
    from core.db import get_async_session

    session_factory = get_async_session(engine)
    test_app.state.db_session_factory = session_factory
    test_app.state.redis = redis_client

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
    """Bootstrap a test org + project + API key — cleaned up by table truncation.

    Under the current contract the admin org bootstrap creates neither an
    API key nor a default project, so this fixture drives the full
    dashboard flow through the real API (see :func:`bootstrap_tenant`):
    org → user → JWT → project → project-scoped API key.  The returned
    ``api_key`` authenticates via the middleware like a real SDK client,
    and ``jwt`` is available for tests that need dashboard-session auth.
    """
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await bootstrap_tenant(isolated_app, client, "Test Org")


@pytest_asyncio.fixture(loop_scope="function")
async def isolated_auth_client(
    isolated_app: Any,
    isolated_org_and_key: dict,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client pre-authenticated, backed by the isolated app."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {isolated_org_and_key['api_key']}"
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def isolated_admin_client(
    isolated_app: Any,
    isolated_org_and_key: dict,
) -> AsyncGenerator[AsyncClient, None]:
    """JWT-authenticated client backed by the isolated app.

    Member JWT — good for project-scoped calls and the
    ``configuration:*`` endpoints.  User management (``/v1/users*``) and
    audit-log reads are platform-admin-gated: use
    ``isolated_superadmin_client`` for those.
    """
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {isolated_org_and_key['jwt']}"
        yield client


async def superadmin_jwt(isolated_app: Any) -> str:
    """Run platform setup and log in as root; return the admin JWT.

    User management (``/v1/users*``) and permission grants are
    platform-admin-gated — the bootstrap org admin JWT (``admin``
    role) gets 403 there.  Tests that create users or grant permissions
    must authenticate with this token instead.

    Idempotent within a test: a second call finds the platform already
    set up (403) and proceeds straight to login.
    """
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        setup = await client.post(
            "/v1/auth/setup", json={"password": "StrongPass1"}  # noqa: S106
        )
        assert setup.status_code in (201, 403), f"platform setup failed: {setup.text}"
        login = await client.post(
            "/v1/auth/login",
            json={"email": "root", "password": "StrongPass1"},  # noqa: S106
        )
        assert login.status_code == 200, f"root login failed: {login.text}"
        token = login.json().get("access_token")
        assert token, "root login issued no access token"
        return str(token)


@pytest_asyncio.fixture(loop_scope="function")
async def isolated_superadmin_client(
    isolated_app: Any,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authed as the platform admin (setup → root login).

    Use for ``/v1/users*`` CRUD and permission-grant calls — the org
    admin JWT gets 403 on those endpoints under the admin gate.
    """
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {await superadmin_jwt(isolated_app)}"
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
