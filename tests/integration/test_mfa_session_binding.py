"""User-dictated integration test #1: MFA email binding.

Covers:
    - Login as user A (MFA on) yields an ``mfa_session_token``.
    - Verifying that token with an *attacker's* email + attacker OTP
      must NOT issue victim tokens (401/403).
    - Verifying with the correct (session-bound) email + OTP succeeds.

No mocks: OTP hashes are planted directly in the real test Redis using
the exact key/hash shape ``generate_and_send`` stores
(``otp:v1:mfa:{email}:hash`` → sha256 hex).  The login challenge, the
OTP check, the session-email binding, and token issuance all run for
real over HTTP.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import subprocess
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

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

_VICTIM_EMAIL = "mfa-victim@example.com"
_ATTACKER_EMAIL = "mfa-attacker@example.com"
_PASSWORD = "Victim-p@ss-1"  # noqa: S105 — test fixture password, not a real credential
_VICTIM_OTP = "123456"
_ATTACKER_OTP = "654321"


def _otp_hash(code: str) -> str:
    """SHA-256 hex digest — the exact shape OtpService stores in Redis."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


async def _seed_dashboard_user(
    app: Any,
    org_id: UUID,
    email: str,
    *,
    mfa: bool = False,
) -> UUID:
    """Create a verified, password-capable dashboard user (test infra)."""
    from repositories.auth_repository import AuthRepository
    from utils.password import hash_password

    async with app.state.db_session_factory() as session:
        repo = AuthRepository(session)
        user = await repo.create_dashboard_user(
            organization_id=org_id,
            email=email,
            password_hash=hash_password(_PASSWORD),
            role="member",
        )
        await repo.mark_email_verified(user.id)
        if mfa:
            await repo.set_mfa_enabled(user.id, True)
        await session.commit()
        return user.id


async def _login(app: Any, email: str) -> Any:
    """POST /v1/auth/login over real HTTP."""
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/auth/login",
            json={"email": email, "password": _PASSWORD},
        )


async def _mfa_token_for(
    app: Any,
    redis_client: Any,
    org_id: UUID,
    user_id: UUID,
    email: str,
) -> str:
    """Get a pending MFA session token for a user.

    Prefers the real login challenge.  Test envs have no SMTP, and
    ``login()`` sends the MFA OTP fail-loud *before* storing the
    session — so when login 5xxes on email delivery, seed the exact
    session shape ``login()`` stores (fixture setup, not a mock) and
    let the real ``mfa_verify`` path run.
    """
    resp = await _login(app, email)
    if resp.status_code == 200:
        body = resp.json()
        assert body.get("requires_mfa") is True, (
            f"MFA-enabled login must challenge, got: {body}"
        )
        assert body.get("mfa_session_token"), "missing mfa_session_token"
        return str(body["mfa_session_token"])
    if 500 <= resp.status_code <= 599:
        token = secrets.token_hex(32)
        await redis_client.setex(
            f"mfa:session:{token}",
            300,
            json.dumps(
                {
                    "user_id": str(user_id),
                    "org_id": str(org_id),
                    "role": "member",
                    "email": email,
                }
            ),
        )
        return token
    pytest.fail(f"unexpected login status {resp.status_code}: {resp.text}")
    raise AssertionError("unreachable")


async def _mfa_verify(app: Any, email: str, otp: str, token: str) -> Any:
    """POST /v1/auth/mfa/verify over real HTTP."""
    transport = asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/auth/mfa/verify",
            json={
                "email": email,
                "otp": otp,
                "mfa_session_token": token,
            },
        )


class TestMfaEmailBinding:
    """MFA OTP is bound to the login session's email, not caller input."""

    @pytest.mark.asyncio
    async def test_attacker_email_and_otp_issue_no_tokens(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        redis_client: Any,
    ) -> None:
        """Attacker email + valid attacker OTP against a victim session → 401.

        Both OTP hashes are planted so the Redis lookup succeeds — the
        attacker code is genuinely valid *for the attacker address*.
        ``mfa_verify`` checks it against the session-bound victim email,
        so verification fails and no tokens are issued.
        """
        org_id = isolated_org_and_key["org_id"]
        victim_id = await _seed_dashboard_user(
            isolated_app, org_id, _VICTIM_EMAIL, mfa=True
        )
        token = await _mfa_token_for(
            isolated_app, redis_client, org_id, victim_id, _VICTIM_EMAIL
        )

        await redis_client.setex(
            f"otp:v1:mfa:{_VICTIM_EMAIL}:hash", 600, _otp_hash(_VICTIM_OTP)
        )
        await redis_client.setex(
            f"otp:v1:mfa:{_ATTACKER_EMAIL}:hash", 600, _otp_hash(_ATTACKER_OTP)
        )

        resp = await _mfa_verify(isolated_app, _ATTACKER_EMAIL, _ATTACKER_OTP, token)
        assert resp.status_code in (401, 403), (
            f"attacker MFA verify must be rejected, got {resp.status_code}: {resp.text}"
        )
        assert resp.json().get("access_token") is None, (
            "no victim tokens may be issued to the attacker"
        )

    @pytest.mark.asyncio
    async def test_correct_email_and_otp_succeeds(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        redis_client: Any,
    ) -> None:
        """Correct session-bound email + OTP → 200 with a token pair."""
        org_id = isolated_org_and_key["org_id"]
        victim_id = await _seed_dashboard_user(
            isolated_app, org_id, _VICTIM_EMAIL, mfa=True
        )
        # Fresh session — a verify attempt single-use burns the token.
        token = await _mfa_token_for(
            isolated_app, redis_client, org_id, victim_id, _VICTIM_EMAIL
        )
        await redis_client.setex(
            f"otp:v1:mfa:{_VICTIM_EMAIL}:hash", 600, _otp_hash(_VICTIM_OTP)
        )

        resp = await _mfa_verify(isolated_app, _VICTIM_EMAIL, _VICTIM_OTP, token)
        assert resp.status_code == 200, (
            f"correct MFA verify must succeed, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("access_token"), "access token missing"
        assert body.get("refresh_token"), "refresh token missing"
