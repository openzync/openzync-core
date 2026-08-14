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
the application lifespan.  See ``openzync.core.db`` for the canonical
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
    lifespan using :func:`openzync.core.db.get_async_session`.

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
        # Invariant: PUBLIC endpoints (signup/join) run with NO org context
        # — ``org_id`` is None here, so no set_config runs and the query is
        # issued as the app DB role.  This is safe only while that role OWNS
        # the tables (verified: ``openzep`` owns ``organizations``/``users``,
        # ``rls_forced = false`` → the table owner bypasses RLS).  If the
        # role ever changes to a non-owner, or FORCE ROW SECURITY is enabled
        # on those tables, signup/join will silently fail — check this
        # invariant on deployment.
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

        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db_superadmin(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency providing an RLS-bypass session for superadmins.

    Same contract as :func:`get_db` (session from ``app.state.db_session_factory``)
    but with ``app.bypass_rls = 'true'`` and ``app.org_id`` set to the
    platform org UUID — so the session can read and mutate rows across
    **all** organizations (org listings, cross-org config, approvals).

    **The bypass is granted only after a DB-verified superadmin check**
    (:func:`dependencies.auth._ensure_superadmin`).  Fail-closed: if the
    check raises (non-superadmin, non-platform org, Redis/DB error), no
    session is yielded and the bypass is never set.  Never enable the
    bypass from the org_id/JWT alone.

    Use ONLY in ``routers/admin_system.py`` and the
    ``approve_org`` transaction.

    Yields:
        An :class:`AsyncSession` with RLS bypassed for the platform admin.

    Raises:
        RuntimeError: If ``db_session_factory`` has not been set on
            ``app.state``.
        HTTPException: 401/403 from the superadmin verification.
    """
    # Lazy import — dependencies/auth.py imports get_db from this module at
    # module level; importing back here at module level would be circular.
    from core.config import PLATFORM_ORG_ID
    from dependencies.auth import _ensure_superadmin

    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError(
            "db_session_factory not found on app.state. "
            "Ensure the application lifespan sets "
            "app.state.db_session_factory = get_async_session(engine)."
        )

    org_id: str | None = getattr(request.state, "org_id", None)
    user_id: str | None = getattr(request.state, "user_id", None)
    if org_id is None or user_id is None:
        raise RuntimeError(
            "get_db_superadmin requires an authenticated JWT session — "
            "org_id and user_id must be present on request.state."
        )

    # ── 1. Verify superadmin (DB-verified role) on a clean session ────────
    async with factory() as verify_session:
        from sqlalchemy import text

        # Same non-bypass RLS context as get_db: the role lookup must run
        # under the role's own row visibility.  If the app DB role ever
        # stops bypassing RLS implicitly (or FORCE ROW SECURITY is enabled),
        # the lookup fails loudly here instead of silently mis-verifying.
        await verify_session.execute(
            text("SELECT set_config('app.org_id', :org_id, true)"),
            {"org_id": org_id},
        )
        await verify_session.execute(
            text("SELECT set_config('app.bypass_rls', 'false', true)"),
        )
        await _ensure_superadmin(request, org_id, user_id, verify_session)

    # ── 2. Open the bypass session only now that the check passed ──────────
    async with factory() as session:
        from sqlalchemy import text

        await session.execute(
            text("SELECT set_config('app.bypass_rls', 'true', true)"),
        )
        await session.execute(
            text("SELECT set_config('app.org_id', :org_id, true)"),
            {"org_id": str(PLATFORM_ORG_ID)},
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ═══════════════════════════════════════════════════════════════════════════════
# Re-export for convenience
# ═══════════════════════════════════════════════════════════════════════════════

__all__: list[str] = ["get_db", "get_db_superadmin"]
