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

    @pytest.mark.asyncio
    async def test_webhook_secret_response_body_never_captured(self) -> None:
        """Webhook secret-bearing responses never get their body audit-captured.

        POST /v1/admin/webhooks returns the one-time signing secret — it must
        not be persisted to audit_logs even when the org enables body capture.
        The audit event itself is still enqueued.
        """
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = self._create_app(mock_arq_pool=mock_pool)
        middleware = AuditMiddleware(app)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/v1/admin/webhooks",
            "headers": [(b"host", b"example.com")],
            "query_string": b"",
            "client": ["10.0.0.1", 54321],
            "state": {"org_id": "org-001", "auth_type": "api_key"},
        }
        app.state.arq_pool = mock_pool
        scope["app"] = app

        secret = "whsec_super_secret_never_persist"  # noqa: S105
        with patch(
            "middleware.audit._resolve_audit_body_capture", return_value=True,
        ):
            await middleware._enqueue_audit(
                scope, "POST", "/v1/admin/webhooks", 201,
                [f'{{"id": 1, "secret": "{secret}"}}'.encode()],
            )

        assert mock_pool.enqueue.called  # event still audited
        details = orjson.loads(mock_pool.enqueue.call_args.kwargs["details"])
        assert "response_body" not in details
        assert secret not in orjson.dumps(details).decode()

    @pytest.mark.asyncio
    async def test_accept_invite_response_body_never_captured(self) -> None:
        """The invite-accept JWT pair is never audit body-captured.

        POST /v1/auth/invites/accept returns a live access + refresh pair.
        The flow is unauthenticated, so ``org_id`` is None on the public
        path and the audit middleware cannot gate on org config alone —
        the route must be excluded by constant.  Defense-in-depth: a bearer
        credential persisted to audit_logs is a standing leak.  The audit
        event itself is still enqueued.
        """
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = self._create_app(mock_arq_pool=mock_pool)
        middleware = AuditMiddleware(app)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/v1/auth/invites/accept",
            "headers": [(b"host", b"example.com")],
            "query_string": b"",
            "client": ["10.0.0.1", 54321],
            # Public path — auth middleware leaves org_id/user_id as None.
            "state": {"org_id": None, "auth_type": None},
        }
        app.state.arq_pool = mock_pool
        scope["app"] = app

        access = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.signature"  # noqa: S105
        refresh = "raw-refresh-token-never-persist"  # noqa: S105
        with patch(
            "middleware.audit._resolve_audit_body_capture", return_value=True,
        ):
            body = (
                f'{{"access_token": "{access}", '
                f'"refresh_token": "{refresh}"}}'
            ).encode()
            await middleware._enqueue_audit(
                scope, "POST", "/v1/auth/invites/accept", 200, [body],
            )

        assert mock_pool.enqueue.called  # event still audited
        details = orjson.loads(mock_pool.enqueue.call_args.kwargs["details"])
        assert "response_body" not in details
        assert access not in orjson.dumps(details).decode()
        assert refresh not in orjson.dumps(details).decode()

    @pytest.mark.asyncio
    async def test_org_code_patch_response_body_never_captured(self) -> None:
        """The org-code PATCH response (live join code) is never body-captured.

        PATCH /admin/org/org-code returns the current join code alongside
        the toggle state — a valid join token, identical in sensitivity to
        the regenerate response.  It must not be persisted to audit_logs
        even when the org enables body capture.  The audit event itself is
        still enqueued.
        """
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = self._create_app(mock_arq_pool=mock_pool)
        middleware = AuditMiddleware(app)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "PATCH",
            "path": "/admin/org/org-code",
            "headers": [(b"host", b"example.com")],
            "query_string": b"",
            "client": ["10.0.0.1", 54321],
            "state": {"org_id": "org-001", "auth_type": "jwt"},
        }
        app.state.arq_pool = mock_pool
        scope["app"] = app

        org_code = "K7M2Q9X4"
        with patch(
            "middleware.audit._resolve_audit_body_capture", return_value=True,
        ):
            body = (
                f'{{"org_code": "{org_code}", "join_enabled": false}}'
            ).encode()
            await middleware._enqueue_audit(
                scope, "PATCH", "/admin/org/org-code", 200, [body],
            )

        assert mock_pool.enqueue.called  # event still audited
        details = orjson.loads(mock_pool.enqueue.call_args.kwargs["details"])
        assert "response_body" not in details
        assert org_code not in orjson.dumps(details).decode()

    @pytest.mark.asyncio
    async def test_response_body_captured_for_non_webhook_route(self) -> None:
        """Body capture still works for ordinary routes (control).

        Proves the webhook exclusion is route-specific and does not disable
        capture for other endpoints.
        """
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock(return_value=None)

        app = self._create_app(mock_arq_pool=mock_pool)
        middleware = AuditMiddleware(app)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/data",
            "headers": [(b"host", b"example.com")],
            "query_string": b"",
            "client": ["10.0.0.1", 54321],
            "state": {"org_id": "org-001", "auth_type": "api_key"},
        }
        app.state.arq_pool = mock_pool
        scope["app"] = app

        with (
            patch("middleware.audit._resolve_audit_body_capture", return_value=True),
            # PII detection loads spaCy NER which is not installed in unit-test
            # environments — no PII present in this body, so skip detection.
            patch("middleware.audit._pii_detector.detect", return_value=[]),
        ):
            await middleware._enqueue_audit(
                scope, "POST", "/data", 200, [b'{"id": 1}'],
            )

        details = orjson.loads(mock_pool.enqueue.call_args.kwargs["details"])
        assert details["response_body"] == '{"id": 1}'
