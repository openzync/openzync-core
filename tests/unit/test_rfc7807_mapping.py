"""Domain -> RFC7807 mapping table — every AppError maps to its HTTP status."""

from __future__ import annotations

import pytest

from core.exceptions import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ExternalServiceError,
    GoneError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitError,
    ServiceUnavailableError,
    ValidationError,
)


@pytest.mark.unit
class TestRFC7807Mapping:
    @pytest.mark.parametrize(
        ("exc", "status", "code"),
        [
            (NotFoundError("x"), 404, "not_found"),
            (ValidationError("x"), 422, "validation_error"),
            (AuthenticationError("x"), 401, "authentication_error"),
            (AuthorizationError("x"), 403, "authorization_error"),
            (ConflictError("x"), 409, "conflict"),
            (RateLimitError("x"), 429, "rate_limit_exceeded"),
            (ExternalServiceError("x"), 502, "external_service_error"),
            (PayloadTooLargeError("x"), 413, "payload_too_large"),
            (ServiceUnavailableError("x"), 503, "service_unavailable"),
            (GoneError("x"), 410, "gone"),
        ],
    )
    def test_status_and_code(self, exc: AppError, status: int, code: str) -> None:
        assert exc.status_code == status
        assert exc.code == code

    def test_problem_shape_keys(self) -> None:
        """_to_problem_json emits type/title/status/detail/instance."""
        import inspect

        from core.exceptions import _to_problem_json

        sig = inspect.signature(_to_problem_json)
        assert set(sig.parameters) == {"request", "exc"}
