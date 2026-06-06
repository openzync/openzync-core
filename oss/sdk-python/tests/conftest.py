"""Test fixtures for the OpenZep Python SDK."""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
import pytest_asyncio
import respx
from httpx import AsyncClient, Response

from openzep.client import AsyncOpenZep, OpenZep


TEST_API_KEY = "mg_test_" + "a" * 64
TEST_BASE_URL = "https://api.openzep.test"


@pytest.fixture
def api_key() -> str:
    return TEST_API_KEY


@pytest.fixture
def base_url() -> str:
    return TEST_BASE_URL


@pytest_asyncio.fixture
async def async_client(api_key: str, base_url: str) -> AsyncOpenZep:
    client = AsyncOpenZep(api_key=api_key, base_url=base_url)
    yield client
    await client.close()


@pytest.fixture
def sync_client(api_key: str, base_url: str) -> OpenZep:
    return OpenZep(api_key=api_key, base_url=base_url)


@pytest.fixture
def mock_http(base_url: str) -> AsyncGenerator[respx.MockRouter, None]:
    """Mock HTTP router for testing — intercepts requests to the test base URL."""
    with respx.mock(base_url=base_url, assert_all_mocked=True) as respx_mock:
        yield respx_mock


def mock_response(data: dict, status: int = 200) -> Response:
    """Create a mock JSON response."""
    return Response(status, json=data)


def mock_error_response(status: int, detail: str, **extra: str) -> Response:
    """Create a mock RFC 7807 error response."""
    body = {
        "type": f"https://errors.openzep.dev/error",
        "title": "Error",
        "status": status,
        "detail": detail,
        **extra,
    }
    return Response(status, json=body)
