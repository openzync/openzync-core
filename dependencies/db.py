"""FastAPI dependency for async database session injection.

Provides :func:`get_db` — a FastAPI dependency that yields an
:class:`AsyncSession <sqlalchemy.ext.asyncio.AsyncSession>` from the
application's session factory, retrieved via ``request.app.state``.

Usage in a router:

    from fastapi import APIRouter, Depends
    from sqlalchemy.ext.asyncio import AsyncSession
    from dependencies.db import get_db

    router = APIRouter()

    @router.get("/items")
    async def list_items(db: AsyncSession = Depends(get_db)):
        ...

The session factory must be set on ``app.state.db_session_factory`` during
the application lifespan.  See ``memgraph.core.db`` for the canonical
lifespan pattern.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides an async DB session.

    Retrieves the session factory from ``request.app.state.db_session_factory``,
    opens a session, and yields it.  On exit, the session is closed.  Commit
    and rollback are **not** handled here — use ``async with session.begin()``
    in the service layer, or call ``await session.commit()`` explicitly.

    The session factory is expected to be an
    ``async_sessionmaker[AsyncSession]`` instance created during the app's
    lifespan using :func:`memgraph.core.db.get_async_session`.

    Yields:
        An :class:`AsyncSession` bound to the application's engine.

    Raises:
        RuntimeError: If ``db_session_factory`` has not been set on
            ``app.state`` (e.g., the lifespan has not run).
    """
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "db_session_factory", None
    )

    if factory is None:
        raise RuntimeError(
            "db_session_factory not found on app.state. "
            "Ensure the application lifespan sets "
            "app.state.db_session_factory = get_async_session(engine)."
        )

    async with factory() as session:
        # ── Apply RLS context from auth middleware ─────────────────────
        # The AuthMiddleware sets PostgreSQL session config for RLS, but
        # that's on a different connection.  We need to re-apply it on
        # *this* connection to ensure RLS policies filter correctly.
        org_id: str | None = getattr(request.state, "org_id", None)
        if org_id is not None:
            from sqlalchemy import text

            await session.execute(
                text("SELECT set_config('app.org_id', :org_id, true)"),
                {"org_id": org_id},
            )
            await session.execute(
                text("SELECT set_config('app.bypass_rls', 'false', true)"),
            )

        yield session


# ═══════════════════════════════════════════════════════════════════════════════
# Re-export for convenience
# ═══════════════════════════════════════════════════════════════════════════════

__all__: list[str] = ["get_db"]
