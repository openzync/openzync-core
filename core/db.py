"""Async SQLAlchemy engine, session factory, and FastAPI dependency.

Usage in a FastAPI application:

    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from core.config import settings
    from core.db import init_db_engine, close_db_engine, get_db

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = init_db_engine(str(settings.DATABASE_URL))
        app.state.db_engine = engine
        app.state.db_session_factory = get_async_session(engine)
        yield  # keep the app alive until shutdown
        await close_db_engine(engine)

    app = FastAPI(lifespan=lifespan)

Then in routers:

    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession
    from core.db import get_db

    @router.get("/items")
    async def list_items(db: AsyncSession = Depends(get_db)):
        ...

``get_db`` itself is re-exported from :mod:`dependencies.db` for backward
compatibility — both import paths resolve to the same RLS-aware dependency,
which applies the tenant isolation context (``app.org_id`` /
``app.bypass_rls``) and handles commit/rollback per request.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from pgvector.asyncpg import register_vector
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.util.concurrency import await_only

from core.rls import RlsAsyncSession

# Re-export the RLS-aware dependency so `from core.db import get_db` resolves
# to the same function as `from dependencies.db import get_db`. The canonical
# definition lives in dependencies/db.py (sets tenant isolation context,
# commits/rolls back); keeping a duplicate here would silently disable RLS.
from dependencies.db import get_db as get_db


def init_db_engine(database_url: str, **kwargs: Any) -> AsyncEngine:
    """Create and return an async SQLAlchemy engine.

    The connection pool is sized from the runtime settings
    (``DB_POOL_SIZE`` / ``DB_MAX_OVERFLOW``) so that every process in a
    scaled-out deployment draws from the same, explicit budget.  The
    sizing rule is ``replicas x (pool_size + max_overflow) <
    max_connections`` — see the Helm chart values for the worked example.

    Args:
        database_url: PostgreSQL connection string.
            **Must** use the ``postgresql+asyncpg://`` scheme.
        **kwargs: Additional engine arguments (override the defaults below).

    Returns:
        A configured :class:`AsyncEngine`.

    Raises:
        ValueError: If ``database_url`` does not use the asyncpg driver.
    """
    if "+asyncpg" not in database_url and database_url.startswith("postgresql"):
        raise ValueError(
            "DATABASE_URL must use the postgresql+asyncpg:// scheme for async "
            "operations. Got: " + database_url
        )

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=kwargs.pop("pool_size", settings.DB_POOL_SIZE),
        max_overflow=kwargs.pop("max_overflow", settings.DB_MAX_OVERFLOW),
        pool_recycle=kwargs.pop("pool_recycle", 3600),
        echo=kwargs.pop("echo", False),
        connect_args={"statement_cache_size": 0},
        **kwargs,
    )
    _register_pgvector_codec(engine)
    return engine


def _register_pgvector_codec(engine: AsyncEngine) -> None:
    """Register the pgvector asyncpg codec on every pooled connection.

    ``episodes.embedding`` / ``facts.embedding`` are native ``VECTOR(768)``
    (migration 0054) mapped with ``pgvector.sqlalchemy.Vector``. asyncpg
    cannot decode the ``vector`` type OID without an explicit codec, so any
    full-ORM read loading the column crashes. This listener registers
    ``pgvector.asyncpg.register_vector`` on each new pooled connection —
    the same hook the SQLAlchemy asyncpg dialect uses for its own
    JSON/JSONB codecs in ``on_connect``. Write paths bind native
    ``list[float]`` via this codec (``CAST(:embedding AS vector(768))``
    only asserts the dimension) — never ``str`` literals, which the
    codec cannot decode.

    The coroutine is driven with
    :func:`sqlalchemy.util.concurrency.await_only`, the documented bridge
    for awaiting async work inside the sync ``"connect"`` event — the same
    helper the asyncpg dialect itself uses for its JSON/JSONB codecs in
    ``on_connect``. It is imported from the module rather than reached via
    the adapted connection's ``await_`` method, which SQLAlchemy removed in
    2.1.x (asyncio-connector rework) and which crashed every pooled
    connection with ``AttributeError`` when the image floated past 2.0.x.

    Recipe verified against SQLAlchemy==2.0.50 (installed: the dialect's
    own ``await_`` is literally ``staticmethod(await_only)``, so this call
    is identical to the previous one) and pgvector's async
    ``register_vector(conn)`` coroutine (installed pgvector 0.5.0; the
    pinned 0.4.2 in requirements.txt shares the signature). 2.1.x
    compatibility follows from ``await_only`` being the documented public
    bridge, but was not executed locally — the deployed boot is the proof.

    Args:
        engine: A genuine :class:`AsyncEngine`. Doubles that patch
            ``create_async_engine`` in unit tests are not real engines, so
            the listener is skipped for them — codec setup only applies to
            live pools, and registration failures always raise loud at
            first connect, never degrade silently.
    """
    if not isinstance(engine, AsyncEngine):
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _connection_record) -> None:
        await_only(register_vector(dbapi_conn._connection))


async def close_db_engine(engine: AsyncEngine) -> None:
    """Dispose of the engine and all connections in its pool.

    Args:
        engine: The async engine to shut down.
    """
    await engine.dispose()


def get_async_session(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to the given engine.

    The factory produces :class:`core.rls.RlsAsyncSession` instances, so
    every session re-binds its tenant context to the transaction that
    follows a commit — the tenant context is bound transaction-locally and
    a bare :class:`AsyncSession` would lose it at the next ``COMMIT``.

    Args:
        engine: An initialised :class:`AsyncEngine`.

    Returns:
        A configured :class:`async_sessionmaker` that produces
        :class:`AsyncSession` instances.
    """
    return async_sessionmaker(
        bind=engine,
        class_=RlsAsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


async def check_db_health(engine: AsyncEngine) -> bool:
    """Check database connectivity by running a simple query.

    Args:
        engine: The application's :class:`AsyncEngine`.

    Returns:
        ``True`` if the database is reachable, ``False`` otherwise.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        return True
    except Exception:
        return False
