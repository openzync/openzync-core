"""Unit tests for services.worker.tasks.base — re-exports and retryability."""

from __future__ import annotations

import httpx
import pytest

from services.worker.tasks.base import (
    ENRICHMENT_ALL,
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_ENTITY_LINKS,
    ENRICHMENT_FACTS,
    ENRICHMENT_OBSERVATIONS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    _is_retryable,
    with_retry,
)


pytestmark = pytest.mark.unit


class TestReExports:
    """Re-exports match the source of truth in workers.tasks.base."""

    def test_enrichment_constants_are_ints(self) -> None:
        """All ENRICHMENT_* constants are integers."""
        for const in (
            ENRICHMENT_ALL,
            ENRICHMENT_CLASSIFICATION,
            ENRICHMENT_EMBEDDING,
            ENRICHMENT_ENTITIES,
            ENRICHMENT_ENTITY_LINKS,
            ENRICHMENT_FACTS,
            ENRICHMENT_OBSERVATIONS,
            ENRICHMENT_STRUCTURED_EXTRACTION,
        ):
            assert isinstance(const, int)

    def test_with_retry_is_callable(self) -> None:
        """with_retry is a callable decorator."""
        assert callable(with_retry)


class TestIsRetryable:
    """_is_retryable classifies HTTP exceptions as transient or permanent."""

    # ── Transient ──────────────────────────────────────────────────────────────

    def test_timeout_exception_is_retryable(self) -> None:
        """httpx.TimeoutException is retryable."""
        assert _is_retryable(httpx.TimeoutException("Connection timed out")) is True

    def test_connect_error_is_retryable(self) -> None:
        """httpx.ConnectError is retryable."""
        assert _is_retryable(httpx.ConnectError("Connection refused")) is True

    @pytest.mark.parametrize("status", [408, 429, 502, 503, 504])
    def test_transient_http_statuses_are_retryable(self, status: int) -> None:
        """HTTP 408, 429, 502, 503, 504 are retryable."""
        request = httpx.Request("GET", "https://example.com")
        response = httpx.Response(status_code=status, request=request)
        exc = httpx.HTTPStatusError("error", request=request, response=response)
        assert _is_retryable(exc) is True

    # ── Permanent ──────────────────────────────────────────────────────────────

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500])
    def test_permanent_http_statuses_not_retryable(self, status: int) -> None:
        """Most 4xx and 500 errors are not retryable."""
        request = httpx.Request("GET", "https://example.com")
        response = httpx.Response(status_code=status, request=request)
        exc = httpx.HTTPStatusError("error", request=request, response=response)
        assert _is_retryable(exc) is False

    # ── Heuristic fallback ─────────────────────────────────────────────────────

    def test_timeout_string_heuristic(self) -> None:
        """Exception containing 'timeout' in string form is retryable."""
        assert _is_retryable(RuntimeError("connection timeout")) is True

    def test_connection_string_heuristic(self) -> None:
        """Exception containing 'connection' in string form is retryable."""
        assert _is_retryable(RuntimeError("connection lost")) is True

    def test_unrelated_exception_not_retryable(self) -> None:
        """Arbitrary exceptions are not retryable."""
        assert _is_retryable(ValueError("invalid input")) is False

    def test_keyboard_interrupt_not_retryable(self) -> None:
        """KeyboardInterrupt is not retryable."""
        assert _is_retryable(KeyboardInterrupt()) is False
