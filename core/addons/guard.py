"""Runtime RLS-bypass guard for addon code — fail-closed privilege ceiling.

Addon tasks run on ordinary tenant sessions and must never escalate to the
platform bypass. :func:`install_rls_guard` wraps :func:`core.rls.set_bypass_rls`
and :func:`core.rls.rls_bypass` with stack-inspecting guards that raise
:class:`AuthorizationError` when the caller frame lives inside an addon
module (``addons.<id>.vN...``).

The asymmetry is deliberate: core modules (``dependencies.db`` et al.)
imported the originals *before* the guard installs, so superadmin flows
keep working; addon modules imported *after* resolve the wrapped versions.
Late ``core.rls.set_bypass_rls(...)`` attribute lookups from addon code hit
the wrapper too.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import core.rls as _rls_module
from core.exceptions import AuthorizationError

logger = logging.getLogger(__name__)

_ADDON_MODULE_PREFIX = "addons."
"""Loaded addon modules always live under this namespace (see loader)."""

_INSTALLED: bool = False
"""Guard installs exactly once per process."""


def _is_addon_caller() -> str | None:
    """Return the addon module name on the call stack, if any.

    Returns:
        The innermost ``addons.*`` module name calling into RLS, or
        ``None`` when the call originates from core code.
    """
    for frame_info in inspect.stack():
        module = inspect.getmodule(frame_info.frame)
        name = getattr(module, "__name__", "") or ""
        if name == __name__ or name == "core.addons.guard":
            continue
        if name.startswith(_ADDON_MODULE_PREFIX):
            return name
    return None


def install_rls_guard() -> None:
    """Install the addon RLS-bypass guard (idempotent, call at startup).

    Wraps ``core.rls.set_bypass_rls`` and ``core.rls.rls_bypass`` in place.
    Addon-originated calls raise :class:`AuthorizationError`; core calls
    pass through untouched.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    _original_set_bypass = _rls_module.set_bypass_rls
    _original_bypass_cls = _rls_module.rls_bypass

    async def guarded_set_bypass_rls(db: Any, enabled: bool = True) -> None:
        """Deny RLS bypass to addon callers, allow core callers."""
        caller = _is_addon_caller()
        if caller is not None:
            logger.error("addon.rls_bypass_denied", extra={"caller": caller})
            raise AuthorizationError(
                "Addon code may not enable the RLS bypass.",
                detail={"caller": caller},
            )
        await _original_set_bypass(db, enabled=enabled)

    def guarded_rls_bypass(db: Any) -> Any:
        """Deny the bypass context manager to addon callers."""
        caller = _is_addon_caller()
        if caller is not None:
            logger.error("addon.rls_bypass_denied", extra={"caller": caller})
            raise AuthorizationError(
                "Addon code may not use the RLS bypass.",
                detail={"caller": caller},
            )
        return _original_bypass_cls(db)

    _rls_module.set_bypass_rls = guarded_set_bypass_rls  # type: ignore[assignment]
    _rls_module.rls_bypass = guarded_rls_bypass  # type: ignore[assignment]
    _INSTALLED = True
    logger.info("addon.rls_guard_installed")


_DB_GUARD_INSTALLED: bool = False
"""DB-dependency guard installs exactly once per process."""


def install_db_guard() -> None:
    """Wrap ``dependencies.db.get_db_superadmin`` against addon callers.

    Load-time scanning (:func:`registry.ensure_no_bypass_dependency`)
    rejects routers that declare the bypass, but an addon could still call
    the dependency directly. This wrapper raises
    :class:`AuthorizationError` when the caller frame lives inside an
    addon module — the same stack-inspection approach as
    :func:`install_rls_guard`. Core modules imported the original before
    this installs, so superadmin flows keep working; addon modules
    imported after resolve the denying wrapper.

    Call once at startup, alongside :func:`install_rls_guard`.
    """
    global _DB_GUARD_INSTALLED
    if _DB_GUARD_INSTALLED:
        return

    import functools

    import dependencies.db as _db_module

    _original = _db_module.get_db_superadmin

    @functools.wraps(_original)
    async def guarded_get_db_superadmin(request: Any) -> Any:
        """Deny the bypass session to addon callers, allow core callers."""
        caller = _is_addon_caller()
        if caller is not None:
            logger.error("addon.db_bypass_denied", extra={"caller": caller})
            raise AuthorizationError(
                "Addon code may not use the superadmin bypass session.",
                detail={"caller": caller},
            )
        async for session in _original(request):
            yield session

    _db_module.get_db_superadmin = guarded_get_db_superadmin  # type: ignore[assignment]
    _DB_GUARD_INSTALLED = True
    logger.info("addon.db_guard_installed")
