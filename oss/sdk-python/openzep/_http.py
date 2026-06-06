"""Async HTTP transport with retry, auth, and error mapping.

Wraps ``httpx.AsyncClient`` with:
- Bearer token authentication
- Exponential backoff retry for 429/5xx responses
- Structured error mapping via ``_errors.raise_on_error``
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from openzep._errors import OpenZepError, raise_on_error

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

RETRYABLE_STATUSES: set[int] = {429, 502, 503, 504}
"""HTTP status codes that trigger automatic retry."""

MAX_RETRIES: int = 3
"""Maximum number of retry attempts before giving up."""

BASE_DELAY: float = 1.0
"""Base delay in seconds for exponential backoff."""

DEFAULT_TIMEOUT: float = 30.0
"""Default per-request timeout in seconds."""


class AsyncHTTPTransport:
    """Low-level async HTTP transport with retry, auth, and error mapping.

    Args:
        api_key: The OpenZep API key (sent as ``Authorization: Bearer <key>``).
        base_url: Base URL of the OpenZep API server.
        timeout: Per-request timeout in seconds.
        max_retries: Maximum retry count for retryable statuses.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:8000",
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"openzep-py/0.1.0",
            },
        )

    async def close(self) -> None:
        """Close the underlying HTTP client connection pool."""
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        json_body: dict | list | None = None,
        params: dict[str, str | int] | None = None,
    ) -> Any:
        """Make an HTTP request with retry and error mapping.

        Args:
            method: HTTP method (``GET``, ``POST``, ``PATCH``, ``DELETE``).
            path: URL path relative to base URL (e.g. ``/v1/users``).
            json_body: Optional JSON-serializable request body.
            params: Optional query parameters.

        Returns:
            Parsed JSON response body.

        Raises:
            OpenZepError: Mapped from the API's RFC 7807 error response.
        """
        url = self._build_url(path)

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method=method,
                    url=url,
                    json=json_body,
                    params=params,
                )
            except httpx.TimeoutException as exc:
                logger.warning("http.timeout", extra={"url": url, "attempt": attempt})
                if attempt < self._max_retries:
                    await self._wait(attempt)
                    continue
                raise OpenZepError(
                    message=f"Request timed out after {self._max_retries} retries: {exc}",
                    status_code=504,
                ) from exc

            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                logger.info(
                    "http.retry",
                    extra={
                        "url": url,
                        "status": response.status_code,
                        "attempt": attempt + 1,
                    },
                )
                await self._wait(attempt)
                continue

            # 204 No Content — no body to parse
            if response.status_code == 204:
                return None

            if response.is_error:
                try:
                    body = response.json()
                except Exception:
                    body = {"detail": response.text}
                raise_on_error(response.status_code, body)

            try:
                return response.json()
            except Exception:
                return {"_raw": response.text}

        # Should not reach here, but safety net:
        raise OpenZepError(
            message=f"Request failed after {self._max_retries} retries",
            status_code=500,
        )

    async def request_stream(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        """Make a streaming HTTP request (no JSON deserialization).

        Used for endpoints that return non-JSON responses (e.g. raw text).
        """
        url = self._build_url(path)
        return await self._client.request(
            method=method,
            url=url,
            json=json_body,
            params=params,
        )

    def _build_url(self, path: str) -> str:
        """Ensure path starts with ``/`` and return full URL."""
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self._base_url}{path}"

    async def _wait(self, attempt: int) -> None:
        """Exponential backoff sleep."""
        delay = BASE_DELAY * (2 ** attempt)
        await asyncio.sleep(delay)
