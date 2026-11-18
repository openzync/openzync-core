"""Bypass-incapable addon sessions — addons can never escalate isolation.

:class:`AddonSession` is the ONLY session class addon code may hold. It
carries a ``_oz_addon_session`` marker that :mod:`core.rls` checks in
:func:`core.rls.set_bypass_rls` / :func:`core.rls.rls_bypass` and rejects
unconditionally — no frame-walking, no caller check to spoof. The
attribute methods below cover direct ``session.set_bypass_rls()`` calls.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from core.exceptions import AuthorizationError
from core.rls import RlsAsyncSession

logger = structlog.get_logger(__name__)


class AddonSession(RlsAsyncSession):
    """An :class:`RlsAsyncSession` that can never bypass RLS."""

    _oz_addon_session: bool = True

    async def set_bypass_rls(self, enabled: bool = True) -> None:  # noqa: D102
        raise AuthorizationError("Addon code may not bypass RLS.")

    def rls_bypass(self) -> Any:  # noqa: D102
        raise AuthorizationError("Addon code may not bypass RLS.")


def addon_session_factory(engine: AsyncEngine) -> async_sessionmaker[AddonSession]:
    """Return a session factory producing ONLY :class:`AddonSession`.

    Args:
        engine: The platform async engine.

    Returns:
        An ``async_sessionmaker`` bound to ``engine`` with
        ``class_=AddonSession``. Core paths keep using ``RlsAsyncSession``.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AddonSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def resolve_addon_factory(ctx: dict[str, Any]) -> async_sessionmaker[AddonSession]:
    """Resolve an addon-only session factory from a worker context.

    Prefers an explicit ``ctx["engine"]``; otherwise rebinds to the same
    engine as ``ctx["db_session_factory"]`` so addon handlers never share
    the core factory's session class.

    Args:
        ctx: ARQ worker context.

    Returns:
        An addon-only session factory.

    Raises:
        RuntimeError: When neither ``engine`` nor ``db_session_factory``
            is present.
    """
    engine = ctx.get("engine")
    if engine is None:
        base = ctx.get("db_session_factory")
        if base is None:
            raise RuntimeError("Addon context requires engine/db_session_factory.")
        engine = getattr(base, "bind", None) or (getattr(base, "kw", {}) or {}).get(
            "bind"
        )
    if engine is None:
        raise RuntimeError("Cannot resolve engine for addon session factory.")
    return addon_session_factory(engine)


__all__ = ["AddonSession", "addon_session_factory", "resolve_addon_factory"]
