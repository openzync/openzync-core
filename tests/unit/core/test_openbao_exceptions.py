"""Unit tests for OpenBao exception hierarchy.

Covers:
- All exception classes instantiate with correct attributes
- Exception hierarchy matches (all inherit from ``OpenBaoError``)
- Error messages propagate correctly
- ``status_code`` attribute is optional
"""

from __future__ import annotations

import pytest

from core.openbao_exceptions import (
    OpenBaoAuthError,
    OpenBaoConnectionError,
    OpenBaoError,
    OpenBaoNamespaceError,
    OpenBaoRateLimitError,
    OpenBaoSecretNotFoundError,
)


@pytest.mark.unit
class TestOpenBaoExceptions:
    """Exception hierarchy and attribute tests."""

    def test_openbao_error_is_base(self) -> None:
        """All OpenBao exceptions inherit from ``OpenBaoError``."""
        assert issubclass(OpenBaoConnectionError, OpenBaoError)
        assert issubclass(OpenBaoAuthError, OpenBaoError)
        assert issubclass(OpenBaoSecretNotFoundError, OpenBaoError)
        assert issubclass(OpenBaoNamespaceError, OpenBaoError)
        assert issubclass(OpenBaoRateLimitError, OpenBaoError)

    def test_openbao_error_inherits_from_exception(self) -> None:
        """``OpenBaoError`` inherits from ``Exception``."""
        assert issubclass(OpenBaoError, Exception)

    def test_openbao_error_message(self) -> None:
        """The error message is accessible via ``str()`` and ``.args``."""
        exc = OpenBaoError("Something went wrong")
        assert str(exc) == "Something went wrong"
        assert exc.args[0] == "Something went wrong"

    def test_openbao_error_default_message(self) -> None:
        """Default message is an empty string."""
        exc = OpenBaoError()
        assert str(exc) == ""

    def test_openbao_error_with_status_code(self) -> None:
        """``status_code`` is stored when provided."""
        exc = OpenBaoError("error", status_code=500)
        assert exc.status_code == 500

    def test_openbao_error_no_status_code(self) -> None:
        """``status_code`` is ``None`` when not provided."""
        exc = OpenBaoError("error")
        assert exc.status_code is None

    def test_connection_error(self) -> None:
        """``OpenBaoConnectionError`` stores message and status code."""
        exc = OpenBaoConnectionError("Cannot reach OpenBao", status_code=502)
        assert str(exc) == "Cannot reach OpenBao"
        assert exc.status_code == 502
        assert isinstance(exc, OpenBaoError)

    def test_auth_error(self) -> None:
        """``OpenBaoAuthError`` stores message and status code."""
        exc = OpenBaoAuthError("Invalid credentials", status_code=401)
        assert str(exc) == "Invalid credentials"
        assert exc.status_code == 401

    def test_secret_not_found_error(self) -> None:
        """``OpenBaoSecretNotFoundError`` stores message and status code."""
        exc = OpenBaoSecretNotFoundError("Secret not found", status_code=404)
        assert str(exc) == "Secret not found"
        assert exc.status_code == 404

    def test_namespace_error(self) -> None:
        """``OpenBaoNamespaceError`` stores message and status code."""
        exc = OpenBaoNamespaceError("Namespace error", status_code=412)
        assert str(exc) == "Namespace error"
        assert exc.status_code == 412

    def test_rate_limit_error(self) -> None:
        """``OpenBaoRateLimitError`` stores message and status code."""
        exc = OpenBaoRateLimitError("Rate limited", status_code=429)
        assert str(exc) == "Rate limited"
        assert exc.status_code == 429

    def test_exception_raised_and_caught_hierarchy(self) -> None:
        """A derived exception is caught by its base type."""
        with pytest.raises(OpenBaoError):
            raise OpenBaoAuthError("test")

    def test_exception_raised_and_caught_base(self) -> None:
        """``OpenBaoError`` is caught as ``Exception``."""
        with pytest.raises(Exception):
            raise OpenBaoError("test")

    def test_all_exceptions_accept_only_message(self) -> None:
        """All exceptions can be created with just a message (no status_code)."""
        OpenBaoConnectionError("msg")
        OpenBaoAuthError("msg")
        OpenBaoSecretNotFoundError("msg")
        OpenBaoNamespaceError("msg")
        OpenBaoRateLimitError("msg")

    def test_all_exceptions_accept_no_args(self) -> None:
        """All exceptions can be created with no arguments."""
        OpenBaoConnectionError()
        OpenBaoAuthError()
        OpenBaoSecretNotFoundError()
        OpenBaoNamespaceError()
        OpenBaoRateLimitError()

    def test_raise_connection_error_isinstance_check(self) -> None:
        """``OpenBaoConnectionError`` is an instance of ``OpenBaoError``."""
        exc = OpenBaoConnectionError("test")
        assert isinstance(exc, OpenBaoError)

    def test_raise_auth_error_isinstance_check(self) -> None:
        """``OpenBaoAuthError`` is an instance of ``OpenBaoError``."""
        exc = OpenBaoAuthError("test")
        assert isinstance(exc, OpenBaoError)

    def test_error_message_contains_helpful_context(self) -> None:
        """Error messages include relevant path/action context for debugging."""
        exc = OpenBaoSecretNotFoundError("[config/data/missing] secret not found")
        assert "config/data/missing" in str(exc)
        assert "secret not found" in str(exc)
