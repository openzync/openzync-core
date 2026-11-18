"""RLS-bypass guard — addon code may never escalate isolation."""

from __future__ import annotations

import inspect
import logging

import core.rls as _rls
from core.exceptions import AuthorizationError

logger = logging.getLogger(__name__)
_INSTALLED = False


def _called_from_addon() -> bool:
    for frame_info in inspect.stack(0):
        filename = frame_info.filename
        # Skip this guard module's own frames — the guarded wrappers live
        # here (core/addons/guard.py) and must never trip the detector.
        if filename.replace("\\", "/").endswith("core/addons/guard.py"):
            continue
        module = inspect.getmodule(frame_info.frame)
        name = getattr(module, "__name__", "") or ""
        # Only the top-level loaded-addon namespace counts. Addon modules
        # load as addons.<slug>.v<major>; core infra is core.addons.* and
        # is excluded by construction.
        if name.startswith("addons."):
            return True
    return False


async def guarded_set_bypass_rls(db, enabled: bool = True) -> None:
    """Deny ``set_bypass_rls`` when any ``addons.*`` frame is on the stack."""
    if enabled and _called_from_addon():
        logger.error("addons.rls_bypass_denied")
        raise AuthorizationError("Addon code may not bypass RLS.")
    await _ORIG_SET_BYPASS(db, enabled)


async def guarded_rls_bypass(db):  # type: ignore[no-untyped-def]
    """Deny the ``rls_bypass`` context manager for addon callers."""
    if _called_from_addon():
        logger.error("addons.rls_bypass_denied")
        raise AuthorizationError("Addon code may not bypass RLS.")
    async with _ORIG_RLS_BYPASS(db):
        yield


_ORIG_SET_BYPASS = _rls.set_bypass_rls
_ORIG_RLS_BYPASS = _rls.rls_bypass


def install_rls_guard() -> None:
    """Monkey-patch RLS bypass entry points (idempotent)."""
    global _INSTALLED
    if _INSTALLED:
        return
    _rls.set_bypass_rls = guarded_set_bypass_rls  # type: ignore[assignment]
    from contextlib import asynccontextmanager

    _rls.rls_bypass = asynccontextmanager(guarded_rls_bypass)  # type: ignore[assignment]
    _INSTALLED = True
    logger.info("addons.rls_guard_installed")


__all__ = ["install_rls_guard"]
