"""Unit tests for the domain exception hierarchy and RFC 7807 handler registration.

Tests validate that:
1. The exception hierarchy is consistent (subclass relationships, status codes).
2. ``register_exception_handlers`` produces correct RFC 7807 Problem Details
   responses for every exception type.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from core.exceptions import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ExternalServiceError,
    InsufficientCreditsError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitError,
    ValidationError,
    register_exception_handlers,
)


class TestExceptionHierarchy:
    """Validate inheritance and default attribute values."""

    @pytest.mark.parametrize(
        ("exc_cls", "expected_status", "expected_code"),
        [
            (NotFoundError, 404, "not_found"),
            (ValidationError, 422, "validation_error"),
            (AuthenticationError, 401, "authentication_error"),
            (AuthorizationError, 403, "authorization_error"),
            (ConflictError, 409, "conflict"),
            (RateLimitError, 429, "rate_limit_exceeded"),
            (InsufficientCreditsError, 402, "insufficient_credits"),
            (ExternalServiceError, 502, "external_service_error"),
            (PayloadTooLargeError, 413, "payload_too_large"),
        ],
    )
    def test_exception_attributes(
        self,
        exc_cls: type[AppError],
        expected_status: int,
        expected_code: str,
    ) -> None:
        """Every concrete exception has the correct status_code and code."""
        exc = exc_cls("test")
        assert exc.status_code == expected_status
        assert exc.code == expected_code

    def test_all_exceptions_are_app_error_subclasses(self) -> None:
        """Every domain exception should inherit from AppError."""
        domain_exceptions = [
            NotFoundError,
            ValidationError,
            AuthenticationError,
            AuthorizationError,
            ConflictError,
            RateLimitError,
            InsufficientCreditsError,
            ExternalServiceError,
            PayloadTooLargeError,
        ]
        for exc_cls in domain_exceptions:
            assert issubclass(
                exc_cls, AppError
            ), f"{exc_cls.__name__} is not a subclass of AppError"

    def test_app_error_defaults(self) -> None:
        """Base AppError has 500 / internal_error."""
        exc = AppError()
        assert exc.status_code == 500
        assert exc.code == "internal_error"
        assert exc.message == "An unexpected error occurred."
        assert exc.detail == {}

    def test_custom_message_and_detail(self) -> None:
        """Exceptions accept a custom message and detail dict."""
        exc = NotFoundError("Custom msg", detail={"resource_id": "abc-123"})
        assert exc.message == "Custom msg"
        assert exc.detail == {"resource_id": "abc-123"}


class TestExceptionHandlers:
    """Validate the RFC 7807 Problem Details response format."""

    @staticmethod
    def _build_test_app() -> FastAPI:
        """Return a minimal FastAPI app with exception handlers registered."""
        app = FastAPI()

        @app.get("/tests/not-found")
        async def raise_not_found() -> None:
            raise NotFoundError("Item not found")

        @app.get("/tests/validation-error")
        async def raise_validation() -> None:
            raise ValidationError("Invalid payload")

        @app.get("/tests/rate-limit")
        async def raise_rate_limit() -> None:
            raise RateLimitError("Slow down")

        register_exception_handlers(app)
        return app

    @pytest.fixture
    async def client(self) -> AsyncClient:
        transport = ASGITransport(app=self._build_test_app())
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.mark.asyncio
    async def test_not_found_returns_404_with_problem_json(self, client: AsyncClient) -> None:
        """404 errors return RFC 7807 Problem Details."""
        resp = await client.get("/tests/not-found")
        assert resp.status_code == 404

        body = resp.json()
        assert body["type"] == "https://errors.openzep.dev/not_found"
        assert body["title"] == "Not Found"
        assert body["status"] == 404
        assert body["detail"] == "Item not found"
        assert body["instance"] == "/tests/not-found"

    @pytest.mark.asyncio
    async def test_validation_error_returns_422(self, client: AsyncClient) -> None:
        """422 errors return RFC 7807 Problem Details."""
        resp = await client.get("/tests/validation-error")
        assert resp.status_code == 422

        body = resp.json()
        assert body["type"] == "https://errors.openzep.dev/validation_error"
        assert body["status"] == 422

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429(self, client: AsyncClient) -> None:
        """429 errors return RFC 7807 Problem Details."""
        resp = await client.get("/tests/rate-limit")
        assert resp.status_code == 429

        body = resp.json()
        assert body["type"] == "https://errors.openzep.dev/rate_limit_exceeded"
        assert body["status"] == 429

    @pytest.mark.asyncio
    async def test_unhandled_app_error_falls_back(self) -> None:
        """A custom AppError subclass that isn't explicitly registered should
        still produce a valid RFC 7807 response via the catch-all handler."""
        app = FastAPI()

        class CustomError(AppError):
            status_code = 499
            code = "custom_error"

        @app.get("/tests/custom")
        async def raise_custom() -> None:
            raise CustomError("Custom problem")

        register_exception_handlers(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/tests/custom")

        assert resp.status_code == 499
        body = resp.json()
        assert body["type"] == "https://errors.openzep.dev/custom_error"
        assert body["status"] == 499
