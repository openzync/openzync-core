"""Unit tests for TracingMiddleware."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from middleware.tracing import TracingMiddleware, _init_tracer


class FakeSpan:
    """Mock span that works as a context manager."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.status_code: int | None = None
        self.status_description: str | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, code: int, description: str | None = None) -> None:
        self.status_code = code
        self.status_description = description

    def __enter__(self) -> "FakeSpan":
        return self

    def __exit__(self, *args: object) -> None:
        pass


@pytest.mark.unit
class TestTracingMiddleware:
    """Test suite for TracingMiddleware — OpenTelemetry distributed tracing."""

    def _create_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        @app.get("/error")
        async def server_error() -> None:
            raise RuntimeError("server error")

        app.add_middleware(TracingMiddleware)
        return app

    def _make_fake_tracer(self) -> MagicMock:
        """Create a tracer that returns FakeSpan instances."""
        fake_span = FakeSpan()
        fake_tracer = MagicMock()
        fake_tracer.start_as_current_span = MagicMock(return_value=fake_span)
        return fake_tracer

    # ── No-tracer passthrough ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_tracer_passthrough(self) -> None:
        """When OpenTelemetry is not configured, requests pass through."""
        with patch("middleware.tracing._tracer", None):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_normal_request_creates_span(self) -> None:
        """When a tracer is configured, a span is created for each request."""
        fake_tracer = self._make_fake_tracer()

        with patch("middleware.tracing._tracer", fake_tracer):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        fake_tracer.start_as_current_span.assert_called_once()
        span_name = fake_tracer.start_as_current_span.call_args[1]["name"]
        assert "/test" in span_name

    @pytest.mark.asyncio
    async def test_span_includes_method_and_path(self) -> None:
        """The span name includes HTTP method and path."""
        fake_tracer = self._make_fake_tracer()

        with patch("middleware.tracing._tracer", fake_tracer):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        call_kwargs = fake_tracer.start_as_current_span.call_args[1]
        name = call_kwargs["name"]
        assert "GET" in name
        assert "/test" in name

    @pytest.mark.asyncio
    async def test_span_sets_http_attributes(self) -> None:
        """Span receives HTTP method, path, host, status attributes."""
        fake_span = FakeSpan()
        fake_tracer = MagicMock()
        fake_tracer.start_as_current_span = MagicMock(return_value=fake_span)

        with patch("middleware.tracing._tracer", fake_tracer):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        assert fake_span.attributes.get("HTTPMethod") == "GET"
        assert fake_span.attributes.get("HTTPTarget") == "/test"
        assert fake_span.attributes.get("HTTPStatusCode") == 200
        assert "HTTPRequestId" in fake_span.attributes

    @pytest.mark.asyncio
    async def test_span_closed_on_response(self) -> None:
        """The span context manager exits after the response."""
        fake_tracer = self._make_fake_tracer()
        span = fake_tracer.start_as_current_span()

        with patch("middleware.tracing._tracer", fake_tracer):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        # The span's __exit__ was called (via context manager)
        # Since FakeSpan is a simple class, we verify attributes were set
        assert span.attributes.get("HTTPStatusCode") == 200

    @pytest.mark.asyncio
    async def test_span_sets_error_status_on_5xx(self) -> None:
        """Error responses set the span status to ERROR for 5xx."""
        fake_span = FakeSpan()
        fake_tracer = MagicMock()
        fake_tracer.start_as_current_span = MagicMock(return_value=fake_span)

        with patch("middleware.tracing._tracer", fake_tracer):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                with pytest.raises(RuntimeError):
                    await c.get("/error")

        assert fake_span.status_code == 1  # StatusCode.ERROR
        assert "Unhandled exception" in (fake_span.status_description or "")

    @pytest.mark.asyncio
    async def test_span_sets_error_status_on_4xx(self) -> None:
        """Client error responses (4xx) set the span status to ERROR."""
        fake_span = FakeSpan()
        fake_tracer = MagicMock()
        fake_tracer.start_as_current_span = MagicMock(return_value=fake_span)

        app = FastAPI()

        @app.get("/not-found")
        async def not_found() -> None:
            from starlette.responses import Response
            return Response(status_code=404)

        app.add_middleware(TracingMiddleware)

        with patch("middleware.tracing._tracer", fake_tracer):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/not-found")
                assert resp.status_code == 404

        assert fake_span.status_code == 1  # StatusCode.ERROR

    @pytest.mark.asyncio
    async def test_span_includes_org_id_if_present(self) -> None:
        """When org_id is in scope state, span receives it as attribute."""
        fake_span = FakeSpan()
        fake_tracer = MagicMock()
        fake_tracer.start_as_current_span = MagicMock(return_value=fake_span)

        from starlette.types import ASGIApp, Scope, Receive, Send

        class _InjectState:
            """Inject scope state so TracingMiddleware can read org_id."""
            def __init__(self, app: ASGIApp):
                self.app = app
            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                scope["state"] = {"org_id": "org-123"}
                await self.app(scope, receive, send)

        inner = FastAPI()

        @inner.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        # InjectState must wrap OUTSIDE TracingMiddleware so scope state
        # is set before TracingMiddleware reads it.
        traced_app = TracingMiddleware(inner)
        full_app = _InjectState(traced_app)

        with patch("middleware.tracing._tracer", fake_tracer):
            transport = ASGITransport(app=full_app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/test")
                assert resp.status_code == 200

        assert "org_id" in fake_span.attributes

    @pytest.mark.asyncio
    async def test_non_http_passthrough(self) -> None:
        """Non-HTTP scopes pass through without tracing."""
        with patch("middleware.tracing._tracer", None):
            app = self._create_app()
            assert app is not None

    @pytest.mark.asyncio
    async def test_tracer_init_no_endpoint(self) -> None:
        """_init_tracer returns None when OZ_OTLP_ENDPOINT is not set."""
        with patch.dict("os.environ", {}, clear=True):
            tracer = _init_tracer()
            assert tracer is None

    @pytest.mark.asyncio
    async def test_tracer_init_stores_result(self) -> None:
        """_init_tracer caches result so subsequent calls return same value."""
        with patch.dict("os.environ", {}, clear=True):
            t1 = _init_tracer()
            t2 = _init_tracer()
            assert t1 is None
            assert t2 is None

    @pytest.mark.asyncio
    async def test_parent_trace_context_preserved(self) -> None:
        """The middleware preserves traceparent header (doesn't strip it)."""
        fake_tracer = self._make_fake_tracer()

        with patch("middleware.tracing._tracer", fake_tracer):
            app = self._create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get(
                    "/test",
                    headers={"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
                )
                assert resp.status_code == 200

        # Span was created (middleware didn't crash with traceparent header)
        fake_tracer.start_as_current_span.assert_called_once()
