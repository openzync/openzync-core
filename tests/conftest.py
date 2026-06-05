"""Root test configuration — shared fixtures for all test levels.

Fixtures that require the application modules to be present (app, async_client,
auth_client, etc.) use ``pytest.importorskip`` so that unit tests can be
collected and run even when the full application stack isn't built yet.

Integration/security tests that depend on a real database or running container
should be skipped with ``@pytest.mark.skip`` at the test level, not here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


# ═══════════════════════════════════════════════════════════════════════════════
# Session-level event loop
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="session")
def event_loop() -> asyncio.AbstractEventLoop:
    """Create a single event loop for the entire test session.

    pytest-asyncio needs a session-scoped loop for ``async_fixture`` s with
    ``scope="session"``.  Without this fixture the default per-function loop
    is used, which can cause ``got Future <Future pending> attached to a
    different loop`` errors when session-scoped fixtures share resources.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Application fixture — skipped if the app module isn't built yet
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def app() -> Any:
    """Create a fresh FastAPI application instance for testing.

    Skips the test (``pytest.skip``) if ``services.api.main`` hasn't been
    implemented yet — this allows unit tests in ``tests/unit/`` to run
    independently of the full application stack.

    Override this fixture in ``tests/integration/conftest.py`` to provide
    a test-specific app wired to a real or containerised database.
    """
    pytest.importorskip("services.api.main", reason="Application not built yet")

    from services.api.main import create_app

    app = create_app()

    # Override the DB session dependency for testing.
    # The override is cleared after each test via ``app.dependency_overrides.clear()``.
    try:
        from dependencies.db import get_db

        async def _override_get_db() -> AsyncGenerator:  # type: ignore[return]
            yield None  # Replace with test session in integration conftest

        app.dependency_overrides[get_db] = _override_get_db
    except ImportError:
        pass  # DB dependency not yet implemented — tests that need it will skip

    yield app

    app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP clients
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def async_client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client backed by the FastAPI test app (ASGI — no Docker needed).

    Uses ``ASGITransport`` so requests are handled in-process without a live
    server.  All tests using this fixture are automatically async.
    """
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def anon_client(async_client: AsyncClient) -> AsyncClient:
    """Alias — an unauthenticated HTTP client."""
    return async_client


@pytest.fixture
async def auth_client(async_client: AsyncClient, test_api_key: str) -> AsyncClient:
    """HTTP client with a valid ``Authorization: Bearer <key>`` header."""
    async_client.headers["Authorization"] = f"Bearer {test_api_key}"
    return async_client


# ═══════════════════════════════════════════════════════════════════════════════
# Auth helpers
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def test_api_key() -> str:
    """Return a synthetic API key for use in auth tests.

    TechLead note: once ``utils.crypto`` is implemented, use
    ``generate_api_key("mg_test_")`` here to produce realistic keys.
    """
    return "mg_test_" + "a" * 64
