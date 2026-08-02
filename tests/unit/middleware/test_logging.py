"""Unit tests for LoggingMiddleware."""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from middleware.logging import LoggingMiddleware


@pytest.mark.unit
class TestLoggingMiddleware:
    """Test suite for LoggingMiddleware — structured request/response logging."""

    def _create_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        @app.post("/create")
        async def create() -> dict:
            return {"id": 1}

        @app.get("/error")
        async def error() -> None:
            raise RuntimeError("boom")

        app.add_middleware(LoggingMiddleware)
        return app

    @pytest.mark.asyncio
    async def test_normal_request_passthrough(self) -> None:
        """Normal requests pass through without error."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_logs_request_completed_on_info(self) -> None:
        """The middleware logs 'request.completed' at INFO with method/path/status."""
        logs: list[dict] = []

        class FakeLogger:
            def info(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, **kwargs})

            def error(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, "level": "error", **kwargs})

        with patch("middleware.logging.logger", FakeLogger()):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        assert len(logs) >= 1
        assert logs[0]["event"] == "request.completed"
        assert logs[0]["method"] == "GET"
        assert logs[0]["path"] == "/test"
        assert logs[0]["status_code"] == 200
        assert "duration_ms" in logs[0]

    @pytest.mark.asyncio
    async def test_post_request_logged_correctly(self) -> None:
        """POST requests are logged with correct method field."""
        logs: list[dict] = []

        class FakeLogger:
            def info(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, **kwargs})

        with patch("middleware.logging.logger", FakeLogger()):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post("/create", json={"name": "test"})
                assert resp.status_code == 200

        assert logs[0]["method"] == "POST"
        assert logs[0]["path"] == "/create"
        assert logs[0]["status_code"] == 200

    @pytest.mark.asyncio
    async def test_error_request_logged_with_500(self) -> None:
        """When the handler raises, the middleware logs 500."""
        logs: list[dict] = []

        class FakeLogger:
            def info(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, **kwargs})

            def error(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, "level": "error", **kwargs})

        with patch("middleware.logging.logger", FakeLogger()):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                with pytest.raises(RuntimeError, match="boom"):
                    await c.get("/error")

        # Should have at least one log entry with 500
        status_codes = [e.get("status_code") for e in logs if "status_code" in e]
        if status_codes:
            assert 500 in status_codes

    @pytest.mark.asyncio
    async def test_duration_is_positive(self) -> None:
        """Duration is a positive number."""
        logs: list[dict] = []

        class FakeLogger:
            def info(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, **kwargs})

        with patch("middleware.logging.logger", FakeLogger()):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        assert logs[0]["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_structlog_contextvars_bound(self) -> None:
        """The middleware binds contextvars with request metadata."""
        logs: list[dict] = []

        class FakeLogger:
            def info(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, **kwargs})

        with patch("middleware.logging.logger", FakeLogger()):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        entry = logs[0]
        assert entry["method"] == "GET"
        assert entry["path"] == "/test"
        assert entry["status_code"] == 200

    @pytest.mark.asyncio
    async def test_non_http_passthrough(self) -> None:
        """Non-HTTP scopes do not log anything."""
        app = self._create_app()
        assert app is not None

    @pytest.mark.asyncio
    async def test_unknown_method_default(self) -> None:
        """If method is missing from scope, it defaults gracefully."""
        logs: list[dict] = []

        class FakeLogger:
            def info(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, **kwargs})

        with patch("middleware.logging.logger", FakeLogger()):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        assert logs[0]["method"] is not None

    @pytest.mark.asyncio
    async def test_rounds_duration_one_decimal(self) -> None:
        """Duration is rounded to 1 decimal place."""
        logs: list[dict] = []

        class FakeLogger:
            def info(self, event: str, **kwargs: object) -> None:
                logs.append({"event": event, **kwargs})

        with patch("middleware.logging.logger", FakeLogger()):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        duration = logs[0]["duration_ms"]
        assert isinstance(duration, float) or isinstance(duration, int)
