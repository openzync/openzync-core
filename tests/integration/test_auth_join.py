"""Integration tests for the org-code join flow — real PostgreSQL.

End-to-end assertions the unit suites cannot make: a real ``organizations``
row drives ``POST /v1/auth/join`` through the actual router, service,
repository and DB session.

Covers the observed contract:
- Valid code for an org with ``join_enabled=False`` → 403
  ``AuthorizationError`` "This organization is not accepting new members",
  NO user created, no OTP sent (the 403 path raises before any user lookup
  or OTP delivery, so no email infrastructure is touched).

The 403 itself proves the code resolved to a real (active) org — an
unknown code would return 422 — so the disabled-vs-unknown distinction is
asserted end-to-end in one request.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.integration.conftest import asgi_transport

pytestmark = pytest.mark.integration

DISABLED_CODE = "DISABLED8"
JOIN_EMAIL = "joiner@openzync.tech"


async def _insert_org(
    engine, *, org_code: str, join_enabled: bool,
) -> None:
    """Insert an org row with a known join code (direct DB — test infra)."""
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO organizations (id, name, plan, org_code, join_enabled) "
                "VALUES (:id, :name, 'free', :code, :enabled)"
            ),
            {
                "id": uuid.uuid4(),
                "name": "Join Test Org",
                "code": org_code,
                "enabled": join_enabled,
            },
        )
        await conn.commit()


async def _user_count(engine, email: str) -> int:
    """Number of users rows for an email — used to prove no user was created."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM users WHERE email = :email"),
            {"email": email},
        )
        return result.scalar_one()


class TestJoinOrgCodeDisabled:
    """POST /v1/auth/join against a real DB row with join_enabled=False."""

    async def test_disabled_org_403_no_user_created(
        self, engine, isolated_app,
    ) -> None:
        """Valid code for a paused org → 403, exact detail, zero users."""
        await _insert_org(engine, org_code=DISABLED_CODE, join_enabled=False)

        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test",
        ) as client:
            resp = await client.post(
                "/v1/auth/join",
                json={
                    "email": JOIN_EMAIL,
                    "password": "SecurePass1",
                    "org_code": DISABLED_CODE,
                },
            )

        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"] == (
            "This organization is not accepting new members"
        )
        # No user created — the 403 fires before any user/email write.
        assert await _user_count(engine, JOIN_EMAIL) == 0
