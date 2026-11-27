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

.. note::

    ``pytest_plugins`` is **not** defined here because pytest 9 no longer
    supports declaring plugins inside a non-top-level conftest.  Security
    tests that need integration fixtures import them explicitly from
    ``tests.integration.conftest``.
"""

# F811: the integration fixtures re-exported below are also requested as
# parameters by the fixtures in this module — same name, no real collision.
# ruff: noqa: F811
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest_asyncio
from httpx import AsyncClient

from tests.integration.conftest import (  # noqa: F401  (fixtures are re-exported)
    asgi_transport,
    bootstrap_tenant,
    db_session,
    engine,
    isolated_app,
    redis_client,
    superadmin_jwt,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture(loop_scope="function")
async def tenants(isolated_app: Any) -> dict[str, dict[str, Any]]:
    """Bootstrap three isolated tenants (A/B/C) via the real API contract.

    Each entry holds ``user_id``, ``user_id``, ``jwt``, ``project_id`` and
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


@pytest_asyncio.fixture(loop_scope="function")
async def superadmin_client(
    isolated_app: Any,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authed as the platform admin — seeds ``/v1/users`` rows.

    User management is platform-admin-gated; the per-tenant member JWTs
    below get 403 there, so all user seeding goes through this client.
    """
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {await superadmin_jwt(isolated_app)}"
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def jwt_client_org_a(
    isolated_app: Any, tenants: dict[str, dict[str, Any]]
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated as org A's member (JWT, default grant set).

    Member JWTs carry every permission except ``members:*`` — they drive
    project-scoped calls and the admin-gated-403 assertions.  User seeding
    goes through ``superadmin_client`` instead.
    """
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {tenants['a']['jwt']}"
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def jwt_client_org_b(
    isolated_app: Any, tenants: dict[str, dict[str, Any]]
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated as org B's member (JWT, default grant set)."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {tenants['b']['jwt']}"
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def jwt_client_org_c(
    isolated_app: Any, tenants: dict[str, dict[str, Any]]
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated as org C's member (JWT, default grant set)."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {tenants['c']['jwt']}"
        yield client
