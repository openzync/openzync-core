"""Unit tests for addon sessions (core/addons/sessions.py).

Pins the bypass-incapable contract: ``AddonSession`` can never escalate
isolation, via either the marker or the request-scope flag. No DB needed —
the denial raises before any connection is touched.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.addons.request_scope import scoped_addon_request
from core.addons.sessions import AddonSession
from core.exceptions import AuthorizationError


class TestAddonSession:
    async def test_set_bypass_rls_denied(self) -> None:
        session = AddonSession()
        with pytest.raises(AuthorizationError, match="may not bypass"):
            await session.set_bypass_rls(True)

    def test_rls_bypass_denied(self) -> None:
        session = AddonSession()
        with pytest.raises(AuthorizationError, match="may not bypass"):
            session.rls_bypass()

    def test_marker_present(self) -> None:
        assert AddonSession._oz_addon_session is True


class TestRlsDenialPaths:
    async def test_addon_session_denied_via_marker(self) -> None:
        from core.rls import set_bypass_rls

        with pytest.raises(AuthorizationError, match="may not bypass"):
            await set_bypass_rls(AddonSession())  # type: ignore[arg-type]

    async def test_request_scope_denied_without_marker(self) -> None:
        from core.rls import set_bypass_rls

        plain = AsyncSession()
        with scoped_addon_request():
            with pytest.raises(AuthorizationError, match="may not bypass"):
                await set_bypass_rls(plain)
        await plain.close()
