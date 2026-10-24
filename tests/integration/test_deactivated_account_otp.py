"""User-dictated integration test #4: deactivated-user OTP.

Covers:
    - Deactivated user OTP send → generic confirmation (anti-enumeration),
      no OTP hash stored in Redis, no tokens issuable.
    - Deactivated user OTP verify → generic invalid-code error, no tokens.
    - Active user unaffected: planted OTP verifies → 200 token pair.

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport.
No mocks: the deactivated send path never reaches the (fail-loud)
email backend, and the active-user OTP hash is planted in the real
Redis using the exact stored shape (sha256 hex).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text as sqltext

from tests.integration.conftest import asgi_transport


def _docker_up() -> bool:
    """Return True when a Docker daemon answers (testcontainers needs it)."""
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        return False
    try:
        subprocess.run(  # noqa: S603 — fixed argv, no shell, no untrusted input
            [docker_bin, "info"],
            capture_output=True,
            timeout=30,
            check=True,
        )
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_up(),
        reason="docker unavailable — PG/Redis testcontainers cannot start; skipped, not mocked",
    ),
]

_DEACTIVATED_EMAIL = "otp-deactivated@example.com"
_ACTIVE_EMAIL = "otp-active@example.com"
_OTP_CODE = "483926"
_PURPOSE = "passwordless_login"


def _otp_key(email: str) -> str:
    """Exact Redis key OtpService uses for a passwordless OTP hash."""
    return f"otp:v1:{_PURPOSE}:{email.lower()}:hash"


async def _seed_user(app: Any, org_id: UUID, email: str) -> UUID:
    """Create a verified dashboard user (test infra)."""
    from repositories.auth_repository import AuthRepository

    async with app.state.db_session_factory() as session:
        repo = AuthRepository(session)
        user = await repo.create_dashboard_user(
            organization_id=org_id,
            email=email,
            role="member",
        )
        await repo.mark_email_verified(user.id)
        await session.commit()
        return user.id


async def _deactivate(app: Any, user_id: UUID) -> None:
    """Flip the user inactive (test infra — mirrors an admin action)."""
    async with (
        app.state.db_session_factory() as session,
        session.begin(),
    ):
        await session.execute(
            sqltext("UPDATE users SET is_active = false WHERE id = :uid"),
            {"uid": str(user_id)},
        )


class TestDeactivatedOtp:
    """Deactivated accounts get the generic invalid-code path, never tokens."""

    @pytest.mark.asyncio
    async def test_deactivated_send_and_verify_issue_nothing(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        redis_client: Any,
    ) -> None:
        """Deactivated send → generic 200 + no stored OTP; verify → 401."""
        user_id = await _seed_user(
            isolated_app, isolated_org_and_key["org_id"], _DEACTIVATED_EMAIL
        )
        await _deactivate(isolated_app, user_id)

        transport = asgi_transport(isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            send = await client.post(
                "/v1/auth/login/otp/send",
                json={"email": _DEACTIVATED_EMAIL},
            )
            assert send.status_code == 200, (
                f"send must stay generic (200), got {send.status_code}: {send.text}"
            )
            assert "If an account exists" in send.json()["message"]

            verify = await client.post(
                "/v1/auth/login/otp/verify",
                json={"email": _DEACTIVATED_EMAIL, "otp": "000000"},
            )
            assert verify.status_code == 401, (
                f"deactivated verify must be 401, "
                f"got {verify.status_code}: {verify.text}"
            )
            assert verify.json().get("access_token") is None

        assert await redis_client.get(_otp_key(_DEACTIVATED_EMAIL)) is None, (
            "no OTP may be stored for a deactivated account"
        )

    @pytest.mark.asyncio
    async def test_active_user_otp_still_verifies(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        redis_client: Any,
    ) -> None:
        """Active user with a valid OTP → 200 token pair (unaffected)."""
        await _seed_user(isolated_app, isolated_org_and_key["org_id"], _ACTIVE_EMAIL)
        await redis_client.setex(
            _otp_key(_ACTIVE_EMAIL),
            600,
            hashlib.sha256(_OTP_CODE.encode("utf-8")).hexdigest(),
        )

        transport = asgi_transport(isolated_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/login/otp/verify",
                json={"email": _ACTIVE_EMAIL, "otp": _OTP_CODE},
            )
        assert resp.status_code == 200, (
            f"active-user verify must succeed, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("access_token"), "access token missing"
        assert body.get("refresh_token"), "refresh token missing"
