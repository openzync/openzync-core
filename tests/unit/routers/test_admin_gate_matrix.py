"""Admin-gate matrix contract test — every admin-gated endpoint, real wiring.

Closes the structural test hole where 7 of 8 admin-router suites override
``require_org_admin`` with a pass-through lambda: those suites would pass
even if a gate were deleted from an endpoint.  This suite never overrides
the gate.  It builds the app with the **real** ``require_org_admin`` /
``require_scope`` / ``require_org_admin_or_self`` dependency chain and
instead patches the infrastructure underneath it:

- ``dependencies.auth.get_org_role`` -> ``"member"`` (the role lookup that
  the real gate consults; see the same patch pattern in
  ``test_admin_org_code.py::_make_member_app`` and
  ``test_rbac.py::TestRequireOrgAdmin``),
- ``app.state.redis`` (required by ``_ensure_org_admin``),
- ``get_db`` (request-scoped session, mocked).

Contract asserted per endpoint:
- member JWT  -> 403  (role check denies)
- no auth     -> 401  (``require_org_id`` denies)

Plus:
- member ON SELF for the ``require_org_admin_or_self`` endpoints -> 200.
- admin role on one representative endpoint -> 200 (proves the 403s come
  from the role check, not from a broken harness).
- a coverage guard: the matrix is diffed against every route that actually
  declares an admin gate, so a NEW admin endpoint added without a matrix
  entry fails this suite.

Excluded by design (documented public / non-admin routes — verified against
router sources):
- ``GET /v1/admin/webhooks/events``  — no auth, public event-type listing.
- ``GET /admin/org/config/defaults`` — no auth, seeded onboarding defaults.
- ``GET /v1/users``, ``GET /v1/users/{user_id}`` — ``require_org_id`` only
  (any authenticated caller), not admin-gated.
- ``GET /v1/auth/registration-status`` — PUBLIC by design (registration
  policy drives the signup UI).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.config import PLATFORM_ORG_ID
from core.exceptions import register_exception_handlers
from dependencies.auth import (
    require_org_admin,
    require_org_admin_or_self,
    require_superadmin,
)
from dependencies.db import get_db
from routers import (
    admin,
    admin_invites,
    admin_metrics,
    admin_org_code,
    admin_org_config,
    admin_organizations,
    admin_quick_actions,
    admin_schemas,
    admin_stats,
    admin_system,
    admin_webhooks,
    audit_log,
    users,
)
from routers.admin import _get_admin_org_service
from routers.admin_org_code import _get_org_service
from routers.users import get_user_summary_service
from schemas.organizations import CreateOrgResponse
from schemas.user_summary import UserSummaryResponse
from services.organization_service import OrgCodeInfo

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
MEMBER_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000003")
ENDPOINT_ID = UUID("00000000-0000-0000-0000-000000000004")
SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000005")
SUPERADMIN_USER_ID = UUID("00000000-0000-0000-0000-0000000000bb")
PROMPT_NAME = "extract_facts_v2"

ALL_ROUTERS = [
    audit_log.router,
    admin.router,
    admin_org_code.router,
    admin_org_config.router,
    admin_webhooks.router,
    admin_organizations.router,
    admin_stats.router,
    admin_metrics.router,
    admin_quick_actions.router,
    admin_schemas.router,
    admin_invites.router,
    admin_system.router,
    users.router,
]

# ── Endpoint table ────────────────────────────────────────────────────────────
# (method, path_template, path_params, query_params)
# ``{user_id}`` in the or_self entries resolves to OTHER_USER_ID (member
# attempting to read another user's data -> 403).

ADMIN_GATED_ENDPOINTS: list[tuple[str, str, dict, dict]] = [
    # audit logs
    ("GET", "/v1/admin/audit-logs", {}, {}),
    # org join code
    ("GET", "/admin/org/org-code", {}, {}),
    ("PATCH", "/admin/org/org-code", {}, {}),
    ("POST", "/admin/org/org-code/regenerate", {}, {}),
    # org config (PATCH/PUT gate via require_scope("admin:write"))
    ("GET", "/admin/org/config", {}, {}),
    ("PATCH", "/admin/org/config", {}, {}),
    ("PUT", "/admin/org/config", {}, {}),
    # webhooks (GET /events is public — excluded)
    ("GET", "/v1/admin/webhooks", {}, {}),
    ("GET", "/v1/admin/webhooks/{endpoint_id}", {"endpoint_id": str(ENDPOINT_ID)}, {}),
    ("POST", "/v1/admin/webhooks", {}, {}),
    ("PATCH", "/v1/admin/webhooks/{endpoint_id}", {"endpoint_id": str(ENDPOINT_ID)}, {}),
    ("DELETE", "/v1/admin/webhooks/{endpoint_id}", {"endpoint_id": str(ENDPOINT_ID)}, {}),
    # organizations — prompts + custom-instructions (all require_org_admin)
    ("GET", "/admin/org/prompts", {}, {}),
    ("GET", "/admin/org/prompts/system", {}, {}),
    ("POST", "/admin/org/prompts/import", {}, {}),
    ("POST", "/admin/org/prompts/{name}/set-default", {"name": PROMPT_NAME}, {}),
    ("GET", "/admin/org/prompts/{name}", {"name": PROMPT_NAME}, {}),
    ("GET", "/admin/org/prompts/{name}/versions", {"name": PROMPT_NAME}, {}),
    ("PUT", "/admin/org/prompts/{name}", {"name": PROMPT_NAME}, {}),
    ("POST", "/admin/org/prompts/{name}/rollback/{version}", {"name": PROMPT_NAME, "version": "1"}, {}),
    ("DELETE", "/admin/org/prompts/{name}", {"name": PROMPT_NAME}, {}),
    ("GET", "/admin/org/custom-instructions", {}, {}),
    ("PUT", "/admin/org/custom-instructions", {}, {}),
    ("DELETE", "/admin/org/custom-instructions", {}, {}),
    # stats
    ("GET", "/v1/admin/stats/org", {}, {}),
    ("GET", "/v1/admin/stats/usage", {}, {}),
    # metrics (real prefix /metrics — see routers/admin_metrics.py)
    ("GET", "/metrics/summary", {}, {}),
    ("GET", "/metrics/query", {}, {"query": "up"}),
    ("GET", "/metrics/targets", {}, {}),
    # quick actions
    ("GET", "/v1/admin/quick-actions", {}, {}),
    # extraction schemas (require_org_admin / require_scope("admin"))
    ("POST", "/v1/admin/schemas", {}, {}),
    ("GET", "/v1/admin/schemas", {}, {}),
    ("GET", "/v1/admin/schemas/{schema_id}", {"schema_id": str(SCHEMA_ID)}, {}),
    ("PUT", "/v1/admin/schemas/{schema_id}", {"schema_id": str(SCHEMA_ID)}, {}),
    ("DELETE", "/v1/admin/schemas/{schema_id}", {"schema_id": str(SCHEMA_ID)}, {}),
    # users — admin-gated mutations
    ("POST", "/v1/users", {}, {}),
    ("PATCH", "/v1/users/{user_id}", {"user_id": str(OTHER_USER_ID)}, {}),
    ("DELETE", "/v1/users/{user_id}", {"user_id": str(OTHER_USER_ID)}, {}),
    ("POST", "/v1/users/{user_id}/summary", {"user_id": str(OTHER_USER_ID)}, {}),
    ("PUT", "/v1/users/{user_id}/summary-instructions", {"user_id": str(OTHER_USER_ID)}, {}),
    ("DELETE", "/v1/users/{user_id}/summary-instructions", {"user_id": str(OTHER_USER_ID)}, {}),
    # users — invite flow (admin only)
    ("POST", "/v1/admin/users/invite", {}, {}),
    (
        "DELETE",
        "/v1/admin/users/invites/{user_id}",
        {"user_id": str(OTHER_USER_ID)},
        {},
    ),
    # users — require_org_admin_or_self: member on ANOTHER user
    ("GET", "/v1/users/{user_id}/summary", {"user_id": str(OTHER_USER_ID)}, {}),
    ("GET", "/v1/users/{user_id}/summary-instructions", {"user_id": str(OTHER_USER_ID)}, {}),
    # platform superadmin — POST /admin/organizations (re-gated bootstrap)
    ("POST", "/admin/organizations", {}, {}),
    # platform superadmin — /admin/system/*
    ("GET", "/admin/system/config", {}, {}),
    ("PATCH", "/admin/system/config", {}, {}),
    ("GET", "/admin/system/settings", {}, {}),
    ("GET", "/admin/system/settings/{key}", {"key": "OZ_DATABASE_URL"}, {}),
    ("GET", "/admin/system/orgs", {}, {}),
    ("GET", "/admin/system/orgs/{org_id}/members", {"org_id": str(ORG_ID)}, {}),
    ("GET", "/admin/system/orgs/{org_id}/config", {"org_id": str(ORG_ID)}, {}),
    ("PATCH", "/admin/system/orgs/{org_id}/config", {"org_id": str(ORG_ID)}, {}),
    ("POST", "/admin/system/orgs/{org_id}/approve", {"org_id": str(ORG_ID)}, {}),
    ("POST", "/admin/system/orgs/{org_id}/reject", {"org_id": str(ORG_ID)}, {}),
    (
        "PATCH",
        "/admin/system/orgs/{org_id}/members/{user_id}/role",
        {"org_id": str(ORG_ID), "user_id": str(OTHER_USER_ID)},
        {},
    ),
]

_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT"})


def _make_app(
    *,
    authenticated: bool,
    auth_org_id: str | None = None,
    auth_user_id: str | None = None,
    auth_role: str = "member",
) -> FastAPI:
    """Build the app with the real admin-gate dependency chain.

    ``authenticated=True`` adds middleware that sets a member JWT session
    on ``request.state`` (same pattern as
    ``test_admin_org_code.py::_make_member_app``).  The gate dependencies
    are NEVER overridden — only their infrastructure is mocked.

    ``auth_org_id``/``auth_user_id``/``auth_role`` customise the session
    (used by the superadmin-200 test, which must present the platform org
    and the ``superadmin`` role).
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.state.redis = AsyncMock()
    app.state.openbao_client = AsyncMock()  # org-config service dep
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    # get_user_summary_service is declared BEFORE the gate in the summary
    # handlers' signatures and calls core.arq.get_arq() (RuntimeError when
    # uninitialised) — mock it so the gate is what decides the outcome.
    app.dependency_overrides[get_user_summary_service] = lambda: AsyncMock()
    # Same for get_invite_service: it pulls get_auth_service (Redis on
    # app.state) and must not execute before the gate rejects.
    from dependencies.services import get_invite_service

    app.dependency_overrides[get_invite_service] = lambda: AsyncMock()

    if authenticated:

        @app.middleware("http")
        async def _member_jwt(request, call_next):
            request.state.org_id = auth_org_id or str(ORG_ID)
            request.state.user_id = auth_user_id or str(MEMBER_USER_ID)
            request.state.auth_type = "jwt"
            request.state.role = auth_role
            request.state.api_key_scopes = []
            return await call_next(request)

    for router in ALL_ROUTERS:
        app.include_router(router)
    return app


def _request_path(path_template: str, path_params: dict) -> str:
    return path_template.format(**path_params)


# ── Member JWT -> 403 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path_template", "path_params", "query_params"),
    ADMIN_GATED_ENDPOINTS,
    ids=[f"{m} {p}" for m, p, _pp, _q in ADMIN_GATED_ENDPOINTS],
)
@pytest.mark.asyncio
async def test_member_jwt_denied_403(
    method: str,
    path_template: str,
    path_params: dict,
    query_params: dict,
) -> None:
    """A member JWT gets 403 on every admin-gated endpoint (real gate)."""
    app = _make_app(authenticated=True)
    path = _request_path(path_template, path_params)

    with patch(
        "dependencies.auth.get_org_role", new=AsyncMock(return_value="member"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.request(
                method,
                path,
                json={} if method in _MUTATING_METHODS else None,
                params=query_params,
            )

    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code} for a member JWT — "
        "admin gate missing or bypassed"
    )


# ── No auth -> 401 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path_template", "path_params", "query_params"),
    ADMIN_GATED_ENDPOINTS,
    ids=[f"{m} {p}" for m, p, _pp, _q in ADMIN_GATED_ENDPOINTS],
)
@pytest.mark.asyncio
async def test_unauthenticated_denied_401(
    method: str,
    path_template: str,
    path_params: dict,
    query_params: dict,
) -> None:
    """No authentication gets 401 on every admin-gated endpoint."""
    app = _make_app(authenticated=False)
    path = _request_path(path_template, path_params)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.request(
            method,
            path,
            json={} if method in _MUTATING_METHODS else None,
            params=query_params,
        )

    assert resp.status_code == 401, (
        f"{method} {path} returned {resp.status_code} with no auth — "
        "require_org_id gate missing or bypassed"
    )


# ── require_org_admin_or_self: member ON SELF -> 200 ──────────────────────────


@pytest.mark.asyncio
async def test_member_self_summary_200() -> None:
    """A member may read their OWN summary (or_self passes without role)."""
    app = _make_app(authenticated=True)
    service = AsyncMock()
    service.get_summary.return_value = UserSummaryResponse(
        user_id=MEMBER_USER_ID, summary="self summary", updated_at=None,
    )
    app.dependency_overrides[get_user_summary_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role", new=AsyncMock(return_value="member"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/v1/users/{MEMBER_USER_ID}/summary")

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_member_self_summary_instructions_200() -> None:
    """A member may read their OWN summary instructions (or_self passes)."""
    app = _make_app(authenticated=True)
    service = AsyncMock()
    service.get_instructions.return_value = []
    app.dependency_overrides[get_user_summary_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role", new=AsyncMock(return_value="member"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/v1/users/{MEMBER_USER_ID}/summary-instructions",
            )

    assert resp.status_code == 200, resp.text


# ── Positive control: admin role passes (harness is sound) ────────────────────


@pytest.mark.asyncio
async def test_admin_role_passes_org_code_200() -> None:
    """Admin role gets 200 through the REAL gate — 403s above are role-driven."""
    app = _make_app(authenticated=True)
    service = AsyncMock()
    service.get_org_code.return_value = OrgCodeInfo("K7M2Q9X4", True)
    app.dependency_overrides[_get_org_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role", new=AsyncMock(return_value="admin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/org-code")

    assert resp.status_code == 200
    assert resp.json() == {"org_code": "K7M2Q9X4", "join_enabled": True}


# ── Positive control: superadmin passes the re-gated bootstrap ────────────────


@pytest.mark.asyncio
async def test_superadmin_role_passes_bootstrap_201() -> None:
    """Superadmin JWT gets 201 through the REAL gate on POST /admin/organizations.

    Proves the re-gated bootstrap endpoint rejects without a superadmin
    token (403/401 above) and succeeds with one.
    """
    app = _make_app(
        authenticated=True,
        auth_org_id=str(PLATFORM_ORG_ID),
        auth_user_id=str(SUPERADMIN_USER_ID),
        auth_role="superadmin",
    )
    service = AsyncMock()
    service.create_organization.return_value = CreateOrgResponse(
        organization_id=UUID("00000000-0000-0000-0000-0000000000cc"),
        organization_name="Acme Corp",
    )
    app.dependency_overrides[_get_admin_org_service] = lambda: service

    with patch(
        "dependencies.auth.get_org_role",
        new=AsyncMock(return_value="superadmin"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/organizations",
                json={"name": "Acme Corp", "plan": "free"},
            )

    assert resp.status_code == 201, resp.text
    assert resp.json()["organization_name"] == "Acme Corp"
    service.create_organization.assert_awaited_once()


# ── Coverage guard: matrix must match every actually-gated route ──────────────


def _admin_gate_on_route(route) -> bool:
    """True if the route declares an admin gate dependency.

    Detects ``require_org_admin``, ``require_org_admin_or_self``,
    ``require_superadmin``, and ``require_scope("admin...")`` closures
    directly on the route.
    """
    for dep in route.dependant.dependencies:
        call = dep.call
        if call in (require_org_admin, require_org_admin_or_self, require_superadmin):
            return True
        if getattr(call, "__name__", "") == "_scope_checker":
            scope = next(
                (
                    cell.cell_contents
                    for cell in call.__closure__  # type: ignore[attr-defined]
                    if isinstance(cell.cell_contents, str)
                ),
                "",
            )
            if scope.startswith("admin"):
                return True
    return False


def _introspect_gated_routes() -> set[tuple[str, str]]:
    gated: set[tuple[str, str]] = set()
    for router in ALL_ROUTERS:
        for route in router.routes:
            if hasattr(route, "dependant") and _admin_gate_on_route(route):
                method = next(iter(route.methods))
                gated.add((method, route.path))
    return gated


def test_matrix_covers_every_admin_gated_route() -> None:
    """The matrix and the routers' real gates agree exactly.

    Fails if a new admin-gated endpoint is added without a matrix entry,
    or if a matrix entry points at a route that is no longer gated.
    """
    matrix = {
        (method, path_template)
        for method, path_template, _path_params, _query_params in ADMIN_GATED_ENDPOINTS
    }
    gated = _introspect_gated_routes()

    missing_from_matrix = gated - matrix
    stale_matrix_entries = matrix - gated
    assert not missing_from_matrix, (
        "admin-gated endpoints NOT covered by the matrix: "
        f"{sorted(missing_from_matrix)}"
    )
    assert not stale_matrix_entries, (
        "matrix entries with no admin-gated route (endpoint removed or "
        f"gate deleted): {sorted(stale_matrix_entries)}"
    )
