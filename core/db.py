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
        yield
        await close_db_engine(engine)

    app = FastAPI(lifespan=lifespan)

Then in routers:

    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession
    from core.db import get_db

    @router.get("/items")
    async def list_items(db: AsyncSession = Depends(get_db)):
        ...
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


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
    )


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an :class:`AsyncSession`.

    The session is read from the session factory attached to
    ``request.app.state.db_session_factory`` during the application lifespan.
    The session is automatically closed when the request finishes.
    Commit/rollback is **not** handled here — use ``async with
    session.begin()`` in service code, or call ``await session.commit()``
    explicitly.

    Usage:

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)): ...

    Yields:
        An :class:`AsyncSession` from the application's engine.

    Raises:
        RuntimeError: If ``db_session_factory`` was not initialised on
            ``app.state`` (i.e. the lifespan did not run).
    """
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError(
            "db_session_factory not found on app.state. "
            "Ensure init_db_engine() was called and app.state.db_session_factory "
            "was set during the application lifespan."
        )
    async with factory() as session:
        yield session


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
