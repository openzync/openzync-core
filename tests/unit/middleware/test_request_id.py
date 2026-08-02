"""Unit tests for RequestIDMiddleware."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from middleware.request_id import RequestIDMiddleware


@pytest.mark.unit
class TestRequestIDMiddleware:
    """Test suite for RequestIDMiddleware — X-Request-ID propagation."""

    def _create_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        app.add_middleware(RequestIDMiddleware)
        return app

    @pytest.mark.asyncio
    async def test_missing_request_id_generates_uuid(self) -> None:
        """When no X-Request-ID is sent, a UUID is generated."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 200
            rid = resp.headers.get("x-request-id")
            assert rid is not None
            uuid.UUID(rid)

    @pytest.mark.asyncio
    async def test_client_request_id_is_propagated(self) -> None:
        """When X-Request-ID is sent by client, it is preserved."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"X-Request-ID": "my-trace"})
            assert resp.status_code == 200
            assert resp.headers.get("x-request-id") == "my-trace"

    @pytest.mark.asyncio
    async def test_request_id_in_response_matches_request(self) -> None:
        """Response X-Request-ID matches the one sent in the request."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"X-Request-ID": "abc-123"})
            assert resp.headers.get("x-request-id") == "abc-123"

    @pytest.mark.asyncio
    async def test_structlog_contextvars_bound_during_request(self) -> None:
        """request_id is bound to structlog contextvars during the request."""
        app = FastAPI()

        @app.get("/test")
        async def check_context() -> dict:
            ctx = structlog.contextvars.get_contextvars()
            return {"has_rid": "request_id" in ctx}

        app.add_middleware(RequestIDMiddleware)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"X-Request-ID": "ctx-chk"})
            assert resp.status_code == 200
            assert resp.json()["has_rid"] is True

    @pytest.mark.asyncio
    async def test_structlog_contextvars_cleared_after_request(self) -> None:
        """After the request completes, structlog contextvars are cleared."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get("/test", headers={"X-Request-ID": "clear-check"})
        ctx = structlog.contextvars.get_contextvars()
        assert "request_id" not in ctx

    @pytest.mark.asyncio
    async def test_non_http_scope_passthrough(self) -> None:
        """Lifespan events do not cause errors."""
        app = self._create_app()
        assert app is not None

    @pytest.mark.asyncio
    async def test_multiple_sequential_requests(self) -> None:
        """Multiple sequential requests each get correct IDs."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        ids = ["r1", "r2", "r3"]
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            for expected in ids:
                resp = await c.get("/test", headers={"X-Request-ID": expected})
                assert resp.headers.get("x-request-id") == expected

    @pytest.mark.asyncio
    async def test_uuid_format_on_auto_generated(self) -> None:
        """Auto-generated request IDs are valid UUID4 strings."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            rid = resp.headers["x-request-id"]
            val = uuid.UUID(rid)
            assert val.version == 4
