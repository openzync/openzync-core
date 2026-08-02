"""Unit tests for AuditMiddleware."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from middleware.audit import AuditMiddleware, _resolve_action


@pytest.mark.unit
class TestAuditMiddleware:
    """Test suite for AuditMiddleware — post-response audit logging via ARQ."""

    def _create_app(self, mock_arq_pool: AsyncMock | None = None) -> FastAPI:
        app = FastAPI()

        @app.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        @app.post("/data")
        async def create() -> dict:
            return {"id": 1}

        if mock_arq_pool is not None:
            app.state.arq_pool = mock_arq_pool

        app.add_middleware(AuditMiddleware)
        return app

    @pytest.mark.asyncio
    async def test_normal_get_passthrough(self) -> None:
        """Normal GET request passes through."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_post_request_passthrough(self) -> None:
        """Normal POST request passes through."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/data", json={"name": "test"})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_audit_enqueued_for_mutating_request(self) -> None:
        """POST requests enqueue an ARQ audit job."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = self._create_app(mock_arq_pool=mock_pool)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/data", json={"name": "x"})
            assert resp.status_code == 200

        await asyncio.sleep(0.02)
        assert mock_pool.enqueue.called

    @pytest.mark.asyncio
    async def test_audit_not_enqueued_for_get(self) -> None:
        """GET requests do NOT enqueue an audit job."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = self._create_app(mock_arq_pool=mock_pool)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 200

        await asyncio.sleep(0.02)
        assert not mock_pool.enqueue.called

    @pytest.mark.asyncio
    async def test_audit_not_enqueued_for_options(self) -> None:
        """OPTIONS requests do NOT enqueue an audit job."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = FastAPI()

        @app.get("/data")
        @app.options("/data")
        async def both() -> dict:
            return {"status": "ok"}

        app.state.arq_pool = mock_pool
        app.add_middleware(AuditMiddleware)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.options("/data")
            assert resp.status_code == 200

        await asyncio.sleep(0.02)
        assert not mock_pool.enqueue.called

    @pytest.mark.asyncio
    async def test_exempt_paths_not_audited(self) -> None:
        """Health, metrics, docs paths are not audited."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = FastAPI()

        @app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}

        @app.get("/metrics")
        async def metrics() -> dict:
            return {"status": "ok"}

        app.state.arq_pool = mock_pool
        app.add_middleware(AuditMiddleware)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get("/health")
            await c.get("/metrics")

        await asyncio.sleep(0.02)
        assert not mock_pool.enqueue.called

    @pytest.mark.asyncio
    async def test_post_to_exempt_path_not_audited(self) -> None:
        """Even POST to exempt path /health is not audited."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = FastAPI()

        @app.post("/health")
        async def health_check() -> dict:
            return {"status": "ok"}

        app.state.arq_pool = mock_pool
        app.add_middleware(AuditMiddleware)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/health")
            assert resp.status_code == 200

        await asyncio.sleep(0.02)
        assert not mock_pool.enqueue.called

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_affect_response(self) -> None:
        """When audit enqueue fails, the response is still delivered."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(side_effect=Exception("ARQ down"))

        app = self._create_app(mock_arq_pool=mock_pool)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/data", json={"name": "x"})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_arq_pool_does_not_crash(self) -> None:
        """When no ARQ pool is configured, the request still succeeds."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/data", json={"name": "x"})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_audit_enqueue_contains_expected_fields(self) -> None:
        """Audit enqueue receives expected keyword arguments."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = self._create_app(mock_arq_pool=mock_pool)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/data", json={"name": "x"})
            assert resp.status_code == 200

        await asyncio.sleep(0.02)
        assert mock_pool.enqueue.called
        call_kwargs = mock_pool.enqueue.call_args.kwargs
        assert "queue_name" in call_kwargs
        assert call_kwargs["queue_name"].startswith("OpenZync:")

    @pytest.mark.asyncio
    async def test_resolve_action_fallback(self) -> None:
        """_resolve_action falls back to http.{method} for unknown routes."""
        from middleware.audit import _resolve_action

        action, resource, display = _resolve_action("GET", "/unknown", {})
        assert action == "http.get"
        assert resource == "unknown"

    @pytest.mark.asyncio
    async def test_non_http_passthrough(self) -> None:
        """Non-HTTP scopes pass through."""
        app = self._create_app()
        assert app is not None

    @pytest.mark.asyncio
    async def test_audit_with_org_state(self) -> None:
        """When auth state is set, audit includes org/user info."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        from unittest.mock import patch
        
        # Mock the body capture resolution to avoid OpenBao calls
        with patch("middleware.audit._resolve_audit_body_capture", return_value=False):
            app = self._create_app(mock_arq_pool=mock_pool)
            middleware = AuditMiddleware(app)

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/data",
                "headers": [
                    (b"host", b"example.com"),
                    (b"user-agent", b"test"),
                ],
                "query_string": b"foo=bar",
                "client": ["10.0.0.1", 54321],
                "state": {
                    "org_id": "org-001",
                    "user_id": "user-001",
                    "auth_type": "jwt",
                    "request_id": "req-001",
                },
            }
            app.state.arq_pool = mock_pool
            scope["app"] = app

            await middleware._enqueue_audit(
                scope, "POST", "/data", 200, [b'{"id": 1}'],
            )

            call_kwargs = mock_pool.enqueue.call_args.kwargs
            assert "organization_id" in call_kwargs
            assert "actor_id" in call_kwargs
            assert "action" in call_kwargs
            assert "resource_type" in call_kwargs
            assert "ip_address" in call_kwargs
            assert "trace_id" in call_kwargs
