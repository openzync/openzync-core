"""Admin-gate matrix contract test — every admin-gated endpoint, real wiring.

Closes the structural test hole where 7 of 8 admin-router suites override
the gate with a pass-through stub: those suites would pass even if a gate
were deleted from an endpoint.  This suite never overrides the gate.  It
builds the app with the **real** ``require_permission`` /
``require_permission`` / ``require_admin`` dependency chains and instead patches the
infrastructure underneath it:

- ``dependencies.auth.get_user_role`` -> ``"member"`` (the role lookup that
  the real gate consults; see the same patch pattern in
  ``test_rbac.py::TestRequirePermission``),
- ``dependencies.auth.get_effective_permissions`` -> empty frozenset (a
  member with no explicit grants is denied),
- ``app.state.redis`` (required by ``_check_permission``),
- ``get_db`` (request-scoped session, mocked).

Contract asserted per endpoint:
- member JWT  -> 403  (permission check denies)
- no auth     -> 401  (``require_user_id`` denies)

Plus:
- member ON SELF for the ``require_permission_or_self`` endpoints -> 200.
- admin role on one representative endpoint -> 200 (proves the 403s come
  from the permission check, not from a broken harness).
- a coverage guard: the matrix is diffed against every route that actually
  declares a permission gate, so a NEW admin endpoint added without a
  matrix entry fails this suite.

Excluded by design (verified against router sources):
- ``GET /v1/admin/webhooks/events``  — no auth, public event-type listing.
- ``GET /admin/platform-config/defaults`` — no auth, seeded onboarding defaults.
- ``GET /v1/auth/registration-status`` — PUBLIC by design (registration
  policy drives the signup UI).
- ``GET /v1/admin/stats``, ``GET /v1/admin/stats/usage``,
  ``GET /metrics/summary``, ``GET /metrics/batch``,
  ``GET /v1/admin/quick-actions``, ``GET /v1/admin/llm-usage`` —
  member-visible (``require_user``): every authenticated user sees them.
  Covered by their own suites (member 200, anonymous 401).
"""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from core.exceptions import register_exception_handlers
from dependencies.auth import require_admin
from dependencies.db import get_db
from routers import admin_custom_instructions, admin_invites, admin_metrics, admin_platform_config, admin_quick_actions, admin_schemas, admin_stats, admin_system, admin_webhooks, audit_log, users
from routers.admin_schemas import _get_schema_service
from routers.users import get_user_summary_service
from schemas.extraction_schemas import PreviewExtractionResponse, SchemaTemplateResponse
from schemas.user_summary import UserSummaryResponse
ORG_ID = UUID('00000000-0000-0000-0000-000000000001')
MEMBER_USER_ID = UUID('00000000-0000-0000-0000-000000000002')
OTHER_USER_ID = UUID('00000000-0000-0000-0000-000000000003')
ENDPOINT_ID = UUID('00000000-0000-0000-0000-000000000004')
SCHEMA_ID = UUID('00000000-0000-0000-0000-000000000005')
ADMIN_USER_ID = UUID('00000000-0000-0000-0000-0000000000bb')
ALL_ROUTERS = [audit_log.router, admin_platform_config.router, admin_webhooks.router, admin_custom_instructions.router, admin_stats.router, admin_metrics.router, admin_quick_actions.router, admin_schemas.router, admin_invites.router, admin_system.router, users.router]
ADMIN_GATED_ENDPOINTS: list[tuple[str, str, dict, dict]] = [('GET', '/v1/admin/audit-logs', {}, {}), ('GET', '/v1/admin/audit-logs/actions', {}, {}), ('GET', '/admin/platform-config', {}, {}), ('PATCH', '/admin/platform-config', {}, {}), ('PUT', '/admin/platform-config', {}, {}), ('POST', '/admin/platform-config/test', {}, {}), ('GET', '/v1/admin/webhooks', {}, {}), ('GET', '/v1/admin/webhooks/{endpoint_id}', {'endpoint_id': str(ENDPOINT_ID)}, {}), ('POST', '/v1/admin/webhooks', {}, {}), ('PATCH', '/v1/admin/webhooks/{endpoint_id}', {'endpoint_id': str(ENDPOINT_ID)}, {}), ('DELETE', '/v1/admin/webhooks/{endpoint_id}', {'endpoint_id': str(ENDPOINT_ID)}, {}), ('GET', '/admin/custom-instructions', {}, {}), ('PUT', '/admin/custom-instructions', {}, {}), ('DELETE', '/admin/custom-instructions', {}, {}), ('GET', '/metrics/targets', {}, {}), ('POST', '/v1/admin/schemas', {}, {}), ('GET', '/v1/admin/schemas', {}, {}), ('GET', '/v1/admin/schemas/templates', {}, {}), ('POST', '/v1/admin/schemas/preview', {}, {}), ('GET', '/v1/admin/schemas/{schema_id}', {'schema_id': str(SCHEMA_ID)}, {}), ('PUT', '/v1/admin/schemas/{schema_id}', {'schema_id': str(SCHEMA_ID)}, {}), ('DELETE', '/v1/admin/schemas/{schema_id}', {'schema_id': str(SCHEMA_ID)}, {}), ('POST', '/v1/users', {}, {}), ('GET', '/v1/users', {}, {}), ('GET', '/v1/users/{user_id}', {'user_id': str(OTHER_USER_ID)}, {}), ('PATCH', '/v1/users/{user_id}', {'user_id': str(OTHER_USER_ID)}, {}), ('DELETE', '/v1/users/{user_id}', {'user_id': str(OTHER_USER_ID)}, {}), ('POST', '/v1/users/{user_id}/summary', {'user_id': str(OTHER_USER_ID)}, {}), ('PUT', '/v1/users/{user_id}/summary-instructions', {'user_id': str(OTHER_USER_ID)}, {}), ('DELETE', '/v1/users/{user_id}/summary-instructions', {'user_id': str(OTHER_USER_ID)}, {}), ('POST', '/v1/admin/users/invite', {}, {}), ('DELETE', '/v1/admin/users/invites/{user_id}', {'user_id': str(OTHER_USER_ID)}, {}), ('GET', '/v1/users/{user_id}/summary', {'user_id': str(OTHER_USER_ID)}, {}), ('GET', '/v1/users/{user_id}/summary-instructions', {'user_id': str(OTHER_USER_ID)}, {}), ('GET', '/admin/system/registration', {}, {}), ('PATCH', '/admin/system/registration', {}, {}), ('PATCH', '/admin/system/users/{user_id}/role', {'user_id': str(OTHER_USER_ID)}, {})]
_MUTATING_METHODS = frozenset({'POST', 'PATCH', 'PUT'})

def _make_app(*, authenticated: bool, auth_user_id: str | None=None, auth_role: str='member') -> FastAPI:
    """Build the app with the real permission-gate dependency chain.

    ``authenticated=True`` adds middleware that sets a member JWT session
    on ``request.state``.  The gate dependencies
    are NEVER overridden — only their infrastructure is mocked.

    ``auth_user_id``/``auth_role`` customise the session
    (used by the admin-200 tests, which present the ``admin`` role).
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.state.redis = AsyncMock()
    app.state.db_session_factory = None
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    from dependencies.db import get_db_admin
    app.dependency_overrides[get_db_admin] = lambda: AsyncMock()
    app.dependency_overrides[get_user_summary_service] = lambda: AsyncMock()
    from dependencies.services import get_invite_service
    app.dependency_overrides[get_invite_service] = lambda: AsyncMock()
    from dependencies.platform_config import get_platform_config as get_platform_config_dep
    from schemas.platform_config import PlatformConfigBase
    app.dependency_overrides[get_platform_config_dep] = lambda: PlatformConfigBase(
        llm_backend="ollama", llm_base_url="http://localhost:11434"
    )
    if authenticated:

        @app.middleware('http')
        async def _member_jwt(request, call_next):
            request.state.user_id = auth_user_id or str(ORG_ID)
            request.state.user_id = auth_user_id or str(MEMBER_USER_ID)
            request.state.auth_type = 'jwt'
            request.state.role = auth_role
            request.state.api_key_permissions = []
            return await call_next(request)
    for router in ALL_ROUTERS:
        app.include_router(router)
    return app

def _request_path(path_template: str, path_params: dict) -> str:
    return path_template.format(**path_params)

@pytest.mark.parametrize(('method', 'path_template', 'path_params', 'query_params'), ADMIN_GATED_ENDPOINTS, ids=[f'{m} {p}' for m, p, _pp, _q in ADMIN_GATED_ENDPOINTS])
@pytest.mark.asyncio
async def test_member_jwt_denied_403(method: str, path_template: str, path_params: dict, query_params: dict) -> None:
    """A member JWT gets 403 on every permission-gated endpoint (real gate)."""
    app = _make_app(authenticated=True)
    path = _request_path(path_template, path_params)
    with patch('dependencies.auth.get_user_role', new=AsyncMock(return_value='member')), patch('dependencies.auth.get_effective_permissions', new=AsyncMock(return_value=frozenset())):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.request(method, path, json={} if method in _MUTATING_METHODS else None, params=query_params)
    assert resp.status_code == 403, f'{method} {path} returned {resp.status_code} for a member JWT — permission gate missing or bypassed'

@pytest.mark.parametrize(('method', 'path_template', 'path_params', 'query_params'), ADMIN_GATED_ENDPOINTS, ids=[f'{m} {p}' for m, p, _pp, _q in ADMIN_GATED_ENDPOINTS])
@pytest.mark.asyncio
async def test_unauthenticated_denied_401(method: str, path_template: str, path_params: dict, query_params: dict) -> None:
    """No authentication gets 401 on every admin-gated endpoint."""
    app = _make_app(authenticated=False)
    path = _request_path(path_template, path_params)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        resp = await client.request(method, path, json={} if method in _MUTATING_METHODS else None, params=query_params)
    assert resp.status_code == 401, f'{method} {path} returned {resp.status_code} with no auth — require_user_id gate missing or bypassed'

@pytest.mark.asyncio
async def test_member_self_summary_200() -> None:
    """A member may read their OWN summary (or_self passes without permission)."""
    app = _make_app(authenticated=True)
    service = AsyncMock()
    service.get_summary.return_value = UserSummaryResponse(user_id=MEMBER_USER_ID, summary='self summary', updated_at=None)
    app.dependency_overrides[get_user_summary_service] = lambda: service
    with patch('dependencies.auth.get_user_role', new=AsyncMock(return_value='member')), patch('dependencies.auth.get_effective_permissions', new=AsyncMock(return_value=frozenset())):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.get(f'/v1/users/{MEMBER_USER_ID}/summary')
    assert resp.status_code == 200, resp.text

@pytest.mark.asyncio
async def test_member_self_summary_instructions_200() -> None:
    """A member may read their OWN summary instructions (or_self passes)."""
    app = _make_app(authenticated=True)
    service = AsyncMock()
    service.get_instructions.return_value = []
    app.dependency_overrides[get_user_summary_service] = lambda: service
    with patch('dependencies.auth.get_user_role', new=AsyncMock(return_value='member')), patch('dependencies.auth.get_effective_permissions', new=AsyncMock(return_value=frozenset())):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.get(f'/v1/users/{MEMBER_USER_ID}/summary-instructions')
    assert resp.status_code == 200, resp.text

@pytest.mark.asyncio
async def test_admin_role_passes_schema_templates_200() -> None:
    """Admin role gets 200 through the REAL gate on GET /templates."""
    app = _make_app(authenticated=True)
    service = MagicMock()
    service.list_templates.return_value = [SchemaTemplateResponse(key='invoice', name='Invoice', description='Invoice template', json_schema={'type': 'object'}, sample_text='sample')]
    app.dependency_overrides[_get_schema_service] = lambda: service
    with patch('dependencies.auth.get_user_role', new=AsyncMock(return_value='admin')):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.get('/v1/admin/schemas/templates')
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]['key'] == 'invoice'

@pytest.mark.asyncio
async def test_admin_role_passes_schema_preview_200() -> None:
    """Admin role gets 200 through the REAL gate on POST /preview."""
    app = _make_app(authenticated=True)
    service = AsyncMock()
    service.preview_extraction.return_value = PreviewExtractionResponse(data={}, validation_errors=[])
    app.dependency_overrides[_get_schema_service] = lambda: service
    with patch('dependencies.auth.get_user_role', new=AsyncMock(return_value='admin')):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post('/v1/admin/schemas/preview', json={'json_schema': {'type': 'object'}, 'sample_text': 'hello world'})
    assert resp.status_code == 200, resp.text
    service.preview_extraction.assert_awaited_once()

def _admin_gate_on_route(route) -> bool:
    """True if the route declares a permission-gate dependency.

    Detects ``require_permission("...")`` / ``require_permission_or_self("...")``
    closures (named ``_permission_checker`` / ``_permission_or_self_checker``),
    ``require_admin`` directly on the route, and the
    ``require_admin_or_self()`` factory closure (named
    ``_admin_or_self_checker`` — the users self-read/self-write gate).
    """
    for dep in route.dependant.dependencies:
        call = dep.call
        if call in (require_admin,):
            return True
        if getattr(call, '__name__', '') in ('_permission_checker', '_permission_or_self_checker', '_admin_or_self_checker'):
            return True
    return False

def _introspect_gated_routes() -> set[tuple[str, str]]:
    gated: set[tuple[str, str]] = set()
    for router in ALL_ROUTERS:
        for route in router.routes:
            if hasattr(route, 'dependant') and _admin_gate_on_route(route):
                method = next(iter(route.methods))
                gated.add((method, route.path))
    return gated

def test_matrix_covers_every_admin_gated_route() -> None:
    """The matrix and the routers' real gates agree exactly.

    Fails if a new permission-gated endpoint is added without a matrix
    entry, or if a matrix entry points at a route that is no longer gated.
    """
    matrix = {(method, path_template) for method, path_template, _path_params, _query_params in ADMIN_GATED_ENDPOINTS}
    gated = _introspect_gated_routes()
    missing_from_matrix = gated - matrix
    stale_matrix_entries = matrix - gated
    assert not missing_from_matrix, f'permission-gated endpoints NOT covered by the matrix: {sorted(missing_from_matrix)}'
    assert not stale_matrix_entries, f'matrix entries with no permission-gated route (endpoint removed or gate deleted): {sorted(stale_matrix_entries)}'