"""Collection-endpoint permission-gate tests — real gate wiring.

Covers the ADR-007 gates added to the collection-level endpoints:

- ``POST /v1/projects``   → ``project:manage``
- ``GET /v1/projects``    → ``project:read``
- ``GET /v1/users``       → ``members:read``
- ``GET /v1/users/{id}``  → ``members:read`` (or-self)
- ``GET /v1/search``      → ``project:read``

Same contract-test approach as ``test_admin_gate_matrix.py``: the REAL
``require_permission`` / ``require_permission_or_self`` chain is never
overridden — only the infrastructure underneath it is mocked
(``get_org_role``, ``get_effective_permissions``, Redis, DB, services).

Contract asserted per endpoint:
- member JWT without the permission  -> 403
- member JWT holding the permission  -> 200 (grant path through the real gate)
- API key holding the permission     -> 200 (API-key branch of the gate)
- ``or_self``: member JWT on SELF    -> 200 without any permission
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from core.exceptions import register_exception_handlers
from dependencies.db import get_db
from schemas.projects import ProjectResponse
from schemas.users import UserListResponse, UserResponseWithStats

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
MEMBER_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000003")


@pytest.fixture(autouse=True)
def _stub_search_service() -> None:
    """Mock ``GlobalSearchService`` for every test in this file.

    The search handler constructs its service inline (not via a
    dependency), so granted-path requests would otherwise execute real
    service code against a mocked DB session.  Denied-path requests never
    reach the handler.
    """
    with patch("routers.global_search.GlobalSearchService") as mock_cls:
        instance = AsyncMock()
        instance.search.return_value = []
        mock_cls.return_value = instance
        yield


# ── Endpoint table ────────────────────────────────────────────────────────────
# (test_id, method, path, json_body, required_permission)
GATED_ENDPOINTS: list[tuple[str, str, str, dict | None, str]] = [
    ("create_project", "POST", "/v1/projects", {"name": "Gate Test"}, "project:manage"),
    ("list_projects", "GET", "/v1/projects", None, "project:read"),
    ("list_users", "GET", "/v1/users", None, "members:read"),
    (
        "get_other_user",
        "GET",
        f"/v1/users/{OTHER_USER_ID}",
        None,
        "members:read",
    ),
    ("global_search", "GET", "/v1/search?q=needle", None, "project:read"),
]


def _make_project_service() -> AsyncMock:
    """Service mock satisfying POST+GET /v1/projects handlers."""
    service = AsyncMock()
    service.create_project.return_value = ProjectResponse(
        id=UUID("00000000-0000-0000-0000-000000000010"),
        name="Gate Test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    service.list_projects.return_value = []
    return service


def _make_user_service() -> AsyncMock:
    """Service mock satisfying GET /v1/users[/{user_id}] handlers."""
    service = AsyncMock()
    service.list_users.return_value = UserListResponse(
        data=[], next_cursor=None, has_more=False
    )
    service.get_user.return_value = UserResponseWithStats(
        id=OTHER_USER_ID,
        external_id="gate_test_user",
        organization_id=ORG_ID,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return service


def _make_app(
    *,
    auth_type: str = "jwt",
    api_key_permissions: list[str] | None = None,
    project_service: AsyncMock | None = None,
    user_service: AsyncMock | None = None,
) -> FastAPI:
    """Build an app mounting all three routers behind the REAL gate."""
    app = FastAPI()
    register_exception_handlers(app)
    app.state.redis = AsyncMock()  # required by _check_permission's JWT branch
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    from routers.global_search import router as search_router
    from routers.projects import _get_project_service
    from routers.projects import router as projects_router
    from routers.users import get_user_service
    from routers.users import router as users_router

    app.dependency_overrides[_get_project_service] = lambda: (
        project_service or _make_project_service()
    )
    app.dependency_overrides[get_user_service] = lambda: (
        user_service or _make_user_service()
    )

    @app.middleware("http")
    async def _mock_auth(request: Request, call_next):
        request.state.org_id = str(ORG_ID)
        if auth_type == "jwt":
            request.state.user_id = str(MEMBER_USER_ID)
        else:
            request.state.user_id = str(MEMBER_USER_ID)
            request.state.api_key_permissions = api_key_permissions or []
        request.state.auth_type = auth_type
        return await call_next(request)

    app.include_router(projects_router)
    app.include_router(users_router)
    app.include_router(search_router)
    return app


async def _request(app: FastAPI, method: str, path: str, json_body: dict | None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, json=json_body)


# ── Member JWT without the permission -> 403 ─────────────────────────────────


@pytest.mark.parametrize(
    ("_name", "method", "path", "json_body", "_perm"),
    GATED_ENDPOINTS,
    ids=[e[0] for e in GATED_ENDPOINTS],
)
@pytest.mark.asyncio
async def test_member_jwt_denied_403(
    _name: str, method: str, path: str, json_body: dict | None, _perm: str
) -> None:
    """A member JWT with no explicit grants gets 403 (real gate, fail-closed)."""
    app = _make_app()
    with (
        patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="member"),
        ),
        patch(
            "dependencies.auth.get_effective_permissions",
            new=AsyncMock(return_value=frozenset()),
        ),
    ):
        resp = await _request(app, method, path, json_body)

    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code} for an unprivileged "
        "member — gate missing or bypassed"
    )


# ── Member JWT holding the permission -> 200 ──────────────────────────────────


@pytest.mark.parametrize(
    ("_name", "method", "path", "json_body", "perm"),
    GATED_ENDPOINTS,
    ids=[e[0] for e in GATED_ENDPOINTS],
)
@pytest.mark.asyncio
async def test_member_jwt_granted_200(
    _name: str, method: str, path: str, json_body: dict | None, perm: str
) -> None:
    """A member JWT whose permissions column holds the grant gets 200."""
    app = _make_app()
    with (
        patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="member"),
        ),
        patch(
            "dependencies.auth.get_effective_permissions",
            new=AsyncMock(return_value=frozenset({perm})),
        ),
    ):
        resp = await _request(app, method, path, json_body)

    # POST /v1/projects returns 201 Created; reads return 200.
    expected = 201 if method == "POST" else 200
    assert resp.status_code == expected, resp.text


# ── API key holding the permission -> 200 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("_name", "method", "path", "json_body", "perm"),
    GATED_ENDPOINTS,
    ids=[e[0] for e in GATED_ENDPOINTS],
)
@pytest.mark.asyncio
async def test_api_key_granted_200(
    _name: str, method: str, path: str, json_body: dict | None, perm: str
) -> None:
    """An API key whose permission list holds the grant gets 200."""
    app = _make_app(auth_type="api_key", api_key_permissions=[perm])
    resp = await _request(app, method, path, json_body)

    # POST /v1/projects returns 201 Created; reads return 200.
    expected = 201 if method == "POST" else 200
    assert resp.status_code == expected, resp.text


# ── or_self: member JWT on SELF -> 200 without any permission ────────────────


@pytest.mark.asyncio
async def test_get_own_user_without_permission_200() -> None:
    """A member may always fetch their OWN record (or_self bypass)."""
    app = _make_app()
    with (
        patch(
            "dependencies.auth.get_org_role",
            new=AsyncMock(return_value="member"),
        ),
        patch(
            "dependencies.auth.get_effective_permissions",
            new=AsyncMock(return_value=frozenset()),
        ),
    ):
        resp = await _request(app, "GET", f"/v1/users/{MEMBER_USER_ID}", None)

    assert resp.status_code == 200, resp.text
