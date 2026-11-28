"""Unit tests for the admin org config router.

Tests ``/admin/platform-config`` CRUD endpoints and the ``/defaults`` endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import (
    get_dashboard_user,
    require_user_id,
)
from dependencies.db import get_db
from routers.admin_platform_config import _get_config_service, router
from schemas.platform_config import (
    PlatformConfigBase,
    PlatformConfigResponse,
)
from services.platform_config_service import PlatformConfigService

USER_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(autouse=True)
def _mock_org_admin_role(monkeypatch) -> None:
    """Resolve the org-admin role lookup for every test in this file.

    GET is gated by ``require_permission("configuration:read")`` and
    PATCH/PUT by ``require_permission("configuration:write")``; both JWT
    paths funnel into ``core.rbac.get_user_role`` via
    ``dependencies.auth._check_permission``.  Patching the role lookup to
    ``"admin"`` keeps the JWT/db/redis dependency chain intact while mocking
    only the role source of truth.
    """

    async def _fake_get_user_role(redis, db, user_id) -> str:
        return "admin"

    monkeypatch.setattr("dependencies.auth.get_user_role", _fake_get_user_role)


def _create_app() -> tuple[FastAPI, AsyncMock]:
    """Build a minimal FastAPI app with the admin org config router."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)
    app.state.redis = AsyncMock()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.user_id = str(USER_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        request.state.api_key_permissions = []
        response = await call_next(request)
        return response

    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_user_id] = lambda: str(USER_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)

    app.include_router(router)
    return app, db_mock


# ── GET /defaults ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_defaults_success() -> None:
    """GET /admin/platform-config/defaults returns 200 with defaults YAML."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    fake_yaml_content = "llm_backend: ollama\nllm_model: llama3\nllm_base_url: http://localhost:11434\n"

    with (
        patch("routers.admin_platform_config.DEFAULTS_PATH") as mock_path,
        patch("routers.admin_platform_config.yaml.safe_load", return_value={
            "llm_backend": "ollama",
            "llm_model": "llama3",
            "llm_base_url": "http://localhost:11434",
        }),
    ):
        mock_path.is_file.return_value = True
        mock_path.open.return_value.__enter__.return_value = fake_yaml_content

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/platform-config/defaults")

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_backend"] == "ollama"
    assert body["llm_model"] == "llama3"


@pytest.mark.asyncio
async def test_get_defaults_500_file_not_found() -> None:
    """GET /admin/platform-config/defaults returns 500 when YAML file is missing."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_platform_config.DEFAULTS_PATH") as mock_path:
        mock_path.is_file.return_value = False

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/platform-config/defaults")

    assert resp.status_code == 500
    assert "not found" in resp.json()["detail"].lower()


# ── GET "" (stored config) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_platform_config_success() -> None:
    """GET /admin/platform-config returns 200 with stored config."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=PlatformConfigService)
    mock_service.get_config_response.return_value = PlatformConfigResponse(
        stored=PlatformConfigBase(
            llm_backend="ollama",
            llm_model="llama3",
            llm_base_url="http://localhost:11434",
        ),
        system_managed_fields=[],
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/platform-config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stored"]["llm_backend"] == "ollama"
    assert body["stored"]["llm_model"] == "llama3"
    assert body["system_managed_fields"] == []
    mock_service.get_config_response.assert_awaited_once_with(USER_ID)


@pytest.mark.asyncio
async def test_get_platform_config_system_managed_fields_always_empty() -> None:
    """GET /admin/platform-config always returns ``system_managed_fields == []``.

    Graph connection details moved to env (``OZ_FALKORDB_URL`` /
    ``OZ_SURREALDB_*``) with no per-user override, so there is nothing
    left to lock — the router hard-overrides the field to ``[]`` even
    when the service layer reports stale managed fields.
    """
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=PlatformConfigService)
    mock_service.get_config_response.return_value = PlatformConfigResponse(
        stored=PlatformConfigBase(llm_backend="ollama", llm_base_url="http://localhost:11434"),
        system_managed_fields=[
            "surrealdb_url", "surrealdb_user", "surrealdb_pass",
        ],
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/platform-config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["system_managed_fields"] == []


# ── PATCH "" ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_platform_config_success() -> None:
    """PATCH /admin/platform-config returns 200 with updated config."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=PlatformConfigService)
    mock_service.update_config.return_value = PlatformConfigBase(
        llm_backend="openai",
        llm_model="gpt-4",
        llm_api_key="sk-test",
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/admin/platform-config",
            json={"llm_backend": "openai", "llm_model": "gpt-4", "llm_api_key": "sk-test"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_backend"] == "openai"
    assert body["llm_model"] == "gpt-4"
    mock_service.update_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_platform_config_partial() -> None:
    """PATCH /admin/platform-config with partial fields works."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=PlatformConfigService)
    mock_service.update_config.return_value = PlatformConfigBase(
        llm_backend="openai",
        llm_api_key="sk-test",
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/admin/platform-config",
            json={"llm_backend": "openai", "llm_api_key": "sk-test"},
        )

    assert resp.status_code == 200
    assert resp.json()["llm_backend"] == "openai"


@pytest.mark.asyncio
async def test_update_platform_config_422_invalid_field() -> None:
    """PATCH /admin/platform-config returns 422 for invalid field values."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=PlatformConfigService)
    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/admin/platform-config",
            json={"llm_temperature": 99.0},  # Out of 0.0–2.0 range
        )

    assert resp.status_code == 422


# ── PUT "" ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replace_org_config_success() -> None:
    """PUT /admin/platform-config returns 200 with replaced config."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=PlatformConfigService)
    mock_service.update_config.return_value = PlatformConfigBase(
        llm_backend="anthropic",
        llm_model="claude-3-opus",
        llm_api_key="sk-test",
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/admin/platform-config",
            json={
                "llm_backend": "anthropic",
                "llm_model": "claude-3-opus",
                "llm_api_key": "sk-test",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_backend"] == "anthropic"
    assert body["llm_model"] == "claude-3-opus"


# ── 401 / 403 auth ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_config_401_unauthenticated() -> None:
    """GET /admin/platform-config returns 401 when unauthenticated."""
    app = FastAPI()
    # Set up app state so get_db and _get_config_service resolve successfully.
    # require_user_id will raise 401 naturally because no middleware sets user_id.
    app.state.db_session_factory = AsyncMock()
    app.state.redis = AsyncMock()
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/platform-config")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_config_requires_scope() -> None:
    """PATCH /admin/platform-config returns 403 without configuration:write."""
    app = FastAPI()
    # require_permission("configuration:write") resolves get_db even for API
    # keys — the dependency is resolved eagerly by FastAPI before the
    # permission check runs.
    from dependencies.db import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock(spec=AsyncSession)

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.user_id = str(USER_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "api_key"
        request.state.api_key_permissions = ["project:read"]  # No configuration:write
        response = await call_next(request)
        return response

    app.dependency_overrides[require_user_id] = lambda: str(USER_ID)
    # require_permission("configuration:write") will check the key's
    # permission list and find only ["project:read"] → 403.
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/admin/platform-config",
            json={"llm_backend": "openai"},
        )

    assert resp.status_code == 403


def _raise_401():
    from fastapi import HTTPException, status

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
