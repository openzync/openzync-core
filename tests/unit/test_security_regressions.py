"""Security regression tests for the 6-finding remediation.

These tests close coverage gaps the fix's own suites left open:

1. ``H4a`` fail-closed: the middleware bypass list is ``{"/health", "/ready"}``
   but the fix only asserted the ``/health`` bypass. Here we assert ``/ready``
   also survives a Redis outage while ordinary paths 503.
2. ``H5`` enumeration residual: ``verify_email`` and ``passwordless_login``
   used to leak missing accounts via ``NotFoundError``. Both flows now raise
   the same ``AuthenticationError`` as a wrong OTP, so the residual is closed;
   these tests pin the closed behavior.

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
from core.exceptions import AuthenticationError
from middleware.rate_limit import RateLimitMiddleware
from repositories.auth_repository import AuthRepository
from schemas.auth import VerifyEmailRequest
from schemas.email import ResetPasswordRequest, VerifyOtpRequest
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
class TestH5EnumerationClosed:
    """H5 residual closed: consume flows no longer leak account existence.

    ``verify_email``, ``passwordless_login`` and ``reset_password`` raise the
    same ``AuthenticationError`` for an unknown email as for a wrong OTP — a
    missing account means no OTP was ever issued for it, so the two cases
    are indistinguishable.  If someone reintroduces ``NotFoundError`` here,
    these tests fail.
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
    async def test_verify_email_unknown_account_raises_auth_error(
        self, service: AuthService
    ) -> None:
        """verify_email is indistinguishable for unknown accounts."""
        service._repo.find_user_by_email.return_value = None
        with pytest.raises(
            AuthenticationError, match="Invalid or expired verification code"
        ):
            await service.verify_email(
                VerifyEmailRequest(email="nobody@acme.com", otp="123456")
            )

    @pytest.mark.asyncio
    async def test_passwordless_login_unknown_account_raises_auth_error(
        self, service: AuthService
    ) -> None:
        """passwordless_login is indistinguishable for unknown accounts."""
        service._repo.find_user_by_email.return_value = None
        with pytest.raises(
            AuthenticationError, match="Invalid or expired login code"
        ):
            await service.passwordless_login(
                VerifyOtpRequest(email="nobody@acme.com", otp="123456")
            )

    @pytest.mark.asyncio
    async def test_reset_password_unknown_account_raises_auth_error(
        self, service: AuthService
    ) -> None:
        """reset_password is indistinguishable for unknown accounts."""
        service._repo.find_user_by_email.return_value = None
        with pytest.raises(
            AuthenticationError, match="Invalid or expired reset code"
        ):
            await service.reset_password(
                ResetPasswordRequest(
                    email="nobody@acme.com",
                    otp="123456",
                    new_password="NewStrong1",  # noqa: S106 test-only dummy
                )
            )
