"""Integration test fixtures — wired to a real PostgreSQL instance.

Expects PostgreSQL at ``localhost:5432`` with credentials from ``.env``.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from core.config import settings
from core.db import get_async_session


@pytest_asyncio.fixture(scope="session")
async def engine():
    """Session-scoped async engine connected to the real test DB."""
    e = create_async_engine(
        str(settings.DATABASE_URL),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
    )
    yield e
    await e.dispose()


@pytest_asyncio.fixture
async def app(engine) -> Any:
    """Create the FastAPI app wired to the real database."""
    from services.api.main import create_app
    from dependencies.db import get_db

    app = create_app()
    session_factory = get_async_session(engine)
    app.state.db_session_factory = session_factory

    async def _get_db_override() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db_override
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def async_client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client backed by the FastAPI test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def org_and_key(app: Any) -> dict:
    """Create a test org + API key via the bootstrap endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/organizations",
            json={"name": "Test Org", "plan": "free"},
        )
        assert resp.status_code == 201, f"Admin bootstrap failed: {resp.text}"
        data = resp.json()
        return {
            "org_id": UUID(data["organization_id"]),
            "api_key": data["api_key"],
        }


@pytest_asyncio.fixture
async def auth_client(app: Any, org_and_key: dict) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client pre-authenticated with a real API key."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {org_and_key['api_key']}"
        yield client
