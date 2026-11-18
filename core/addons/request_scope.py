"""Addon request scope — marks requests served by addon routes.

A :class:`~contextvars.ContextVar` flag set by the addon route gate
(:func:`dependencies.addons.require_addon_enabled`). :mod:`core.rls`
refuses ``set_bypass_rls`` / ``rls_bypass`` while the flag is set, so
addon HTTP handlers running on a core ``RlsAsyncSession`` cannot
escalate isolation either. This module imports nothing from the app —
no import-cycle risk.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_in_addon_request: ContextVar[bool] = ContextVar(
    "oz_in_addon_request", default=False
)


def addon_request_active() -> bool:
    """Whether the current context is serving an addon route.

    Returns:
        ``True`` inside ``/v1/ext/*`` requests (gate entered), including
        ``asyncio`` tasks spawned in-request (context propagation).
    """
    return _in_addon_request.get()


def enter_addon_request() -> Token[bool]:
    """Mark the current context as an addon request.

    Returns:
        The reset token for :func:`exit_addon_request`.
    """
    return _in_addon_request.set(True)


def exit_addon_request(token: Token[bool]) -> None:
    """Clear the addon-request mark.

    Args:
        token: The token returned by :func:`enter_addon_request`.
    """
    _in_addon_request.reset(token)


@contextmanager
def scoped_addon_request() -> Iterator[None]:
    """Mark an addon-request span (sync context manager)."""
    token = enter_addon_request()
    try:
        yield
    finally:
        exit_addon_request(token)


__all__ = [
    "addon_request_active",
    "enter_addon_request",
    "exit_addon_request",
    "scoped_addon_request",
]
