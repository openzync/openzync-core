"""Shared fixtures for cross-tenant security tests.

Reuses the integration-test infrastructure (``tests.integration.conftest``):
real Postgres + Redis via testcontainers, the real FastAPI app, and the
real ``bootstrap_tenant`` flow (org → user → JWT → project → API key).

On top of that this module bootstraps **three** isolated tenants (A/B/C)
per test and exposes one API-key-authenticated client per tenant, so the
tests exercise the real middleware gates
(``require_project_membership``, ``require_permission[_or_self]``) with
real cross-org UUIDs.  No new container infrastructure — isolation comes
from the ``isolated_app`` table-truncation teardown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest_asyncio
from httpx import AsyncClient

from tests.integration.conftest import asgi_transport, bootstrap_tenant

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytest_plugins = ("tests.integration.conftest",)


@pytest_asyncio.fixture(loop_scope="function")
async def tenants(isolated_app: Any) -> dict[str, dict[str, Any]]:
    """Bootstrap three isolated tenants (A/B/C) via the real API contract.

    Each entry holds ``org_id``, ``user_id``, ``jwt``, ``project_id`` and
    ``api_key`` — all real UUIDs/credentials created through
    :func:`bootstrap_tenant`.  Function-scoped: the ``isolated_app``
    truncation teardown wipes all three orgs after each test.
    """
    bootstrapped: dict[str, dict[str, Any]] = {}
    transport = asgi_transport(isolated_app)
    for label in ("a", "b", "c"):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            bootstrapped[label] = await bootstrap_tenant(
                isolated_app, client, f"Cross Tenant Org {label.upper()}"
            )
    return bootstrapped


@pytest_asyncio.fixture(loop_scope="function")
async def auth_client_org_a(
    isolated_app: Any, tenants: dict[str, dict[str, Any]]
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated as org A (project-scoped API key)."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {tenants['a']['api_key']}"
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def auth_client_org_b(
    isolated_app: Any, tenants: dict[str, dict[str, Any]]
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated as org B (project-scoped API key)."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {tenants['b']['api_key']}"
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def auth_client_org_c(
    isolated_app: Any, tenants: dict[str, dict[str, Any]]
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated as org C (project-scoped API key)."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {tenants['c']['api_key']}"
        yield client
