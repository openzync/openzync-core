"""Unit tests for RateLimitMiddleware."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from middleware.rate_limit import RateLimitMiddleware


@pytest.mark.unit
class TestRateLimitMiddleware:
    """Test suite for RateLimitMiddleware — sliding-window rate limiting."""

    def _make_mock_redis(self, pipeline_result: list | None = None) -> AsyncMock:
        """Create a mock Redis client with a pipeline that returns given results."""
        mock_redis = AsyncMock(spec=Redis)
        mock_pipe = AsyncMock()
        mock_pipe.zremrangebyscore = AsyncMock(return_value=None)
        mock_pipe.zcard = AsyncMock(return_value=0)
        mock_pipe.zadd = MagicMock(return_value=None)
        mock_pipe.expire = MagicMock(return_value=None)
        mock_pipe.execute = AsyncMock(return_value=pipeline_result or [None, 1, 1, True])
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        mock_redis.ping = AsyncMock(return_value=True)
        return mock_redis

    def _create_prod_settings(self) -> object:
        """Create production settings so rate limiting is enforced."""
        import core.config as cfg
        return cfg.Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
            REDIS_URL="redis://localhost:6379/1",
            SECRET_KEY="a" * 32,
            WEBHOOK_SIGNING_SECRET="b" * 32,
            ENVIRONMENT="production",
            RATE_LIMIT_IP_MAX=1,
            RATE_LIMIT_WINDOW_SEC=60,
        )

    def _create_app(
        self,
        mock_redis: AsyncMock | None = None,
    ) -> FastAPI:
        app = FastAPI()

        @app.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        @app.get("/health")
        async def health() -> dict:
            return {"status": "healthy"}

        @app.options("/test")
        async def echo_opt() -> dict:
            return {"status": "ok"}

        app.add_middleware(RateLimitMiddleware)

        if mock_redis is not None:
            app.state.redis = mock_redis

        return app

    @pytest.mark.asyncio
    async def test_development_bypasses_rate_limit(self) -> None:
        """In development environment, rate limiting is bypassed."""
        import core.config as cfg
        settings = cfg.Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
            REDIS_URL="redis://localhost:6379/1",
            SECRET_KEY="a" * 32,
            WEBHOOK_SIGNING_SECRET="b" * 32,
            ENVIRONMENT="development",
        )
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_under_limit_passes(self) -> None:
        """When under the rate limit, the request passes through."""
        settings = self._create_prod_settings()
        mock_redis = self._make_mock_redis([None, 1, 1, True])
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app(mock_redis=mock_redis)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_over_limit_returns_429(self) -> None:
        """When over the rate limit, returns HTTP 429."""
        settings = self._create_prod_settings()
        # zcard returns 2 > max_requests=1 -> over limit
        mock_redis = self._make_mock_redis([None, 2, 1, True])
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app(mock_redis=mock_redis)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_rate_limit_headers_present(self) -> None:
        """Rate-limit headers are present on the response when under limit."""
        settings = self._create_prod_settings()
        mock_redis = self._make_mock_redis([None, 1, 1, True])
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app(mock_redis=mock_redis)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200
                headers = resp.headers
                assert "x-ratelimit-limit" in headers, f"Headers: {dict(headers)}"
                assert "x-ratelimit-remaining" in headers
                assert "x-ratelimit-reset" in headers

    @pytest.mark.asyncio
    async def test_over_limit_has_retry_after(self) -> None:
        """429 response includes Retry-After header and RFC 7807 body."""
        settings = self._create_prod_settings()
        mock_redis = self._make_mock_redis([None, 2, 1, True])
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app(mock_redis=mock_redis)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 429
                assert "retry-after" in resp.headers
                body = resp.json()
                assert body["type"] == "https://errors.openzync.tech/rate_limit_exceeded"
                assert body["title"] == "Too Many Requests"

    @pytest.mark.asyncio
    async def test_health_endpoint_bypasses(self) -> None:
        """Health/ready endpoints bypass rate limiting."""
        settings = self._create_prod_settings()
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/health")
                assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_redis_fail_closed(self) -> None:
        """When Redis is not configured, request is rejected with 503 (fail-closed)."""
        settings = self._create_prod_settings()
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app(mock_redis=None)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 503, (
                    f"Expected 503, got {resp.status_code}: {resp.text}"
                )
                body = resp.json()
                assert body["type"] == "https://errors.openzync.tech/rate_limit_unavailable"
                assert body["title"] == "Service Unavailable"
                assert body["status"] == 503
                assert body["instance"] == "/test"

    @pytest.mark.asyncio
    async def test_redis_ping_fails_fail_closed(self) -> None:
        """When Redis ping fails, request is rejected with 503 (fail-closed)."""
        settings = self._create_prod_settings()
        bad_redis = AsyncMock(spec=Redis)
        bad_redis.ping = AsyncMock(side_effect=ConnectionError("redis down"))
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app(mock_redis=bad_redis)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_rate_limit_check_fails_fail_closed(self) -> None:
        """When the window check raises, request is rejected with 503 (fail-closed)."""
        settings = self._create_prod_settings()
        mock_redis = self._make_mock_redis()
        mock_redis.pipeline.return_value.execute = AsyncMock(
            side_effect=ConnectionError("redis down mid-check")
        )
        with patch("middleware.rate_limit.get_settings", return_value=settings):
            app = self._create_app(mock_redis=mock_redis)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 503, (
                    f"Expected 503, got {resp.status_code}: {resp.text}"
                )

    @pytest.mark.asyncio
    async def test_org_quota_read_fails_fail_closed(self) -> None:
        """Org-quota read failure rejects the request with 503 (fail-closed)."""
        settings = self._create_prod_settings()
        mock_redis = self._make_mock_redis()
        with (
            patch(
                "middleware.rate_limit.get_settings",
                return_value=settings,
            ),
            patch(
                "middleware.rate_limit._get_org_rate_limit",
                side_effect=ConnectionError("redis down"),
            ),
        ):
            app = self._create_app(mock_redis=mock_redis)

            # Set scope["state"]["org_id"] the way AuthMiddleware would.
            @app.middleware("http")
            async def _fake_auth(request, call_next):
                request.state.org_id = "org-123"
                return await call_next(request)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 503

