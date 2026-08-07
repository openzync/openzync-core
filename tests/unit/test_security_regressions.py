"""Security regression tests for the 6-finding remediation.

These tests close coverage gaps the fix's own suites left open:

1. ``H4a`` fail-closed: the middleware bypass list is ``{"/health", "/ready"}``
   but the fix only asserted the ``/health`` bypass. Here we assert ``/ready``
   also survives a Redis outage while ordinary paths 503.
2. ``H5`` enumeration residual: ``verify_email`` and ``passwordless_login``
   still distinguish missing accounts (``NotFoundError``). This test pins the
   current behavior so the residual stays visible and tracked.

All other findings (H2 rotation, H4d lockout, C2 bootstrap gate, C3 webhook
secrets, H3 idempotency) are covered by the fix's own suites — see
``test_auth_service.py``, ``middleware/test_auth_throttle.py``,
``middleware/test_rate_limit.py``, ``routers/test_admin.py``,
``services/test_webhook_service.py``, ``test_idempotency_service.py``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import core.config as cfg
from core.exceptions import NotFoundError
from middleware.rate_limit import RateLimitMiddleware
from repositories.auth_repository import AuthRepository
from schemas.auth import VerifyEmailRequest
from schemas.email import VerifyOtpRequest
from services.auth_service import AuthService


def _prod_settings() -> cfg.Settings:
    """Production settings so rate limiting is enforced."""
    return cfg.Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
        REDIS_URL="redis://localhost:6379/1",
        SECRET_KEY="a" * 32,
        WEBHOOK_SIGNING_SECRET="b" * 32,
        ENVIRONMENT="production",
        RATE_LIMIT_IP_MAX=1,
        RATE_LIMIT_WINDOW_SEC=60,
    )


def _rl_app() -> FastAPI:
    """App with rate limiting, an ordinary path, and both liveness routes."""
    app = FastAPI()

    @app.get("/test")
    async def echo() -> dict:
        return {"status": "ok"}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy"}

    @app.get("/ready")
    async def ready() -> dict:
        return {"status": "ready"}

    app.add_middleware(RateLimitMiddleware)
    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h4a_ready_bypasses_when_redis_down() -> None:
    """H4a: fail-closed 503s must never take down /ready or /health.

    Redis is completely absent (``mock_redis=None`` ⇒ middleware treats it as
    unconfigured). Ordinary paths 503; liveness routes still answer 200.
    """
    with patch("middleware.rate_limit.get_settings", return_value=_prod_settings()):
        app = _rl_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            ready_resp = await c.get("/ready")
            health_resp = await c.get("/health")
            app_resp = await c.get("/test")

    assert ready_resp.status_code == 200, ready_resp.text
    assert health_resp.status_code == 200, health_resp.text
    assert app_resp.status_code == 503, app_resp.text


@pytest.mark.unit
class TestH5ResidualEnumeration:
    """H5 residual: two flows still leak account existence.

    The H5 fix made signup/forgot_password/resend_otp/login-otp-send
    indistinguishable for existing vs missing emails, but ``verify_email`` and
    ``passwordless_login`` still raise ``NotFoundError`` for unknown accounts.

    TODO(SEC-xxx): fold these two flows into the generic-response pattern —
    ticket opened to track the residual; do NOT change service code until the
    follow-up lands (existing tests in test_auth_service.py already pin this).
    """

    @pytest.fixture
    def service(self) -> AuthService:
        return AuthService(
            repo=AsyncMock(spec=AuthRepository),
            otp_service=AsyncMock(),
            redis=AsyncMock(),
            email_service=AsyncMock(),
            bao_client=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_verify_email_still_404s_unknown_account(
        self, service: AuthService
    ) -> None:
        """verify_email leaks a missing account via NotFoundError."""
        service._repo.find_user_by_email.return_value = None
        with pytest.raises(NotFoundError, match="Dashboard user not found"):
            await service.verify_email(
                VerifyEmailRequest(email="nobody@acme.com", otp="123456")
            )

    @pytest.mark.asyncio
    async def test_passwordless_login_still_404s_unknown_account(
        self, service: AuthService
    ) -> None:
        """passwordless_login leaks a missing account via NotFoundError."""
        service._repo.find_user_by_email.return_value = None
        with pytest.raises(NotFoundError, match="Dashboard user not found"):
            await service.passwordless_login(
                VerifyOtpRequest(email="nobody@acme.com", otp="123456")
            )
