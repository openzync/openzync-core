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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Re-export the RLS-aware dependency so `from core.db import get_db` resolves
# to the same function as `from dependencies.db import get_db`. The canonical
# definition lives in dependencies/db.py (sets tenant isolation context,
# commits/rolls back); keeping a duplicate here would silently disable RLS.
from dependencies.db import get_db as get_db


def init_db_engine(database_url: str, **kwargs: Any) -> AsyncEngine:
    """Create and return an async SQLAlchemy engine.

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

    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=kwargs.pop("pool_size", 20),
        max_overflow=kwargs.pop("max_overflow", 10),
        pool_recycle=kwargs.pop("pool_recycle", 3600),
        echo=kwargs.pop("echo", False),
        connect_args={"statement_cache_size": 0},
        **kwargs,
    )
    return engine


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

    Args:
        engine: An initialised :class:`AsyncEngine`.

    Returns:
        A configured :class:`async_sessionmaker` that produces
        :class:`AsyncSession` instances.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
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
