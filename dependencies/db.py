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

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from core.rls import (
    init_context,
    release_tenant_context,
    set_bypass_rls,
    set_tenant_context,
)

logger = logging.getLogger(__name__)


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides an async DB session.

    Retrieves the session factory from ``request.app.state.db_session_factory``,
    opens a session, and yields it.  On exit, the session is closed.

    **Commits.**  The request's *data* is the service layer's job — use
    ``async with session.begin()`` in the service layer, or call
    ``await session.commit()`` explicitly.  This dependency commits the
    request's data once, after the handler returns.

    The RLS tenant context is bound **transaction-locally**
    (``core.rls``), so it is discarded by any commit — including the
    commit this dependency makes at the end of the request.  The session
    is an :class:`core.rls.RlsAsyncSession`, which re-binds the context to
    the transaction that follows every commit, so a mid-request commit
    never leaves the session un-tenanted.

    The session factory is expected to be an
    ``async_sessionmaker[AsyncSession]`` instance created during the app's
    lifespan using :func:`openzync.core.db.get_async_session`.

    Yields:
        An :class:`AsyncSession` bound to the application's engine.

    RLS context behavior by ``request.state.org_id`` value:

    * ``None`` — public endpoint: the context is initialised with an
      **empty** tenant, so no org-scoped row is visible and no
      org-scoped write can succeed.
    * ``""`` / whitespace-only — fail-closed, treated exactly like
      ``None`` (empty tenant, warning logged).
    * Valid UUID string — ``app.org_id`` set to that organization.

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
        # ── Apply the RLS context ────────────────────────────
        # The context is bound TRANSACTION-LOCALLY: PostgreSQL discards
        # it when the transaction ends, so it can never survive onto a
        # pooled connection that the next request will use.  A statement
        # that runs without a bound context sees no rows at all — the
        # policies are fail-closed — so a lost context can never widen a
        # result set.
        #
        # The price is that a mid-request COMMIT discards the context.
        # ``RlsAsyncSession`` pays that price: it remembers the context
        # and re-applies it to the session's next transaction, so the
        # session keeps its own tenant on whichever connection it next
        # uses.
        org_id: str | None = getattr(request.state, "org_id", None)
        if org_id is not None and not org_id.strip():
            logger.warning(
                "skipping empty app.org_id RLS context: path=%s",
                request.url.path,
            )
            org_id = None
        try:
            await init_context(session, org_id)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await release_tenant_context(session)


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

    The bypass is bound **transaction-locally** (``core.rls``), so it is
    discarded by any commit, including a mid-request one.  The session is
    an :class:`core.rls.RlsAsyncSession`, which re-binds the whole
    context (platform org + bypass) to the transaction that follows every
    commit, so the bypass survives a mid-request commit and cannot
    outlive the transaction.

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

    # ── 1. Verify superadmin (DB-verified role) on a clean session
    async with factory() as verify_session:
        # Same non-bypass RLS context as get_db: the role lookup must run
        # under the role's own row visibility.  Under FORCE ROW LEVEL
        # SECURITY the lookup either finds the superadmin row (the session
        # is bound to the platform org) or fails loudly here instead of
        # silently mis-verifying.
        try:
            await init_context(verify_session, org_id)
            await _ensure_superadmin(request, org_id, user_id, verify_session)
        finally:
            await release_tenant_context(verify_session)

    # ── 2. Open the bypass session only now that the check passed
    async with factory() as session:
        # Transaction-local scope: the bypass cannot outlive the
        # transaction, so it cannot stand on a pooled connection that the
        # next request will use.  ``RlsAsyncSession`` re-binds it to the
        # transaction that follows every commit, so the bypass survives a
        # mid-request commit too.
        await set_tenant_context(session, PLATFORM_ORG_ID)
        await set_bypass_rls(session, True)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await release_tenant_context(session)


# ═══════════════════════════════════════════════════════════════════════════════
# Re-export for convenience
# ═══════════════════════════════════════════════════════════════════════════════

__all__: list[str] = ["get_db", "get_db_superadmin"]
