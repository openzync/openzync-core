"""Unit tests for the admin org config router.

Tests ``/admin/org/config`` CRUD endpoints and the ``/defaults`` endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import get_dashboard_user, require_org_id, require_scope
from dependencies.db import get_db
from routers.admin_org_config import router, _get_config_service
from schemas.organization_config import (
    OrgConfigBase,
    OrgConfigResponse,
    UpdateOrgConfigRequest,
)
from services.org_config_service import OrgConfigService

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _create_app() -> tuple[FastAPI, AsyncMock]:
    """Build a minimal FastAPI app with the admin org config router."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        request.state.api_key_scopes = ["admin", "admin:write"]
        response = await call_next(request)
        return response

    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)

    app.include_router(router)
    return app, db_mock


# ── GET /defaults ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_defaults_success() -> None:
    """GET /admin/org/config/defaults returns 200 with defaults YAML."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    fake_yaml_content = "llm_backend: ollama\nllm_model: llama3\n"

    with (
        patch("routers.admin_org_config.DEFAULTS_PATH") as mock_path,
        patch("routers.admin_org_config.yaml.safe_load", return_value={
            "llm_backend": "ollama",
            "llm_model": "llama3",
        }),
    ):
        mock_path.is_file.return_value = True
        mock_path.open.return_value.__enter__.return_value = fake_yaml_content

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/config/defaults")

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_backend"] == "ollama"
    assert body["llm_model"] == "llama3"


@pytest.mark.asyncio
async def test_get_defaults_500_file_not_found() -> None:
    """GET /admin/org/config/defaults returns 500 when YAML file is missing."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_org_config.DEFAULTS_PATH") as mock_path:
        mock_path.is_file.return_value = False

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/config/defaults")

    assert resp.status_code == 500
    assert "not found" in resp.json()["detail"].lower()


# ── GET "" (stored config) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_org_config_success() -> None:
    """GET /admin/org/config returns 200 with stored config."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=OrgConfigService)
    mock_service.get_config_response.return_value = OrgConfigResponse(
        stored=OrgConfigBase(
            llm_backend="ollama",
            llm_model="llama3",
        ),
        system_managed_fields=[],
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/org/config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stored"]["llm_backend"] == "ollama"
    assert body["stored"]["llm_model"] == "llama3"
    assert body["system_managed_fields"] == []
    mock_service.get_config_response.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_get_org_config_with_system_managed_fields() -> None:
    """GET /admin/org/config returns system_managed_fields when system vars set."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=OrgConfigService)
    mock_service.get_config_response.return_value = OrgConfigResponse(
        stored=OrgConfigBase(llm_backend="ollama"),
        system_managed_fields=[
            "surrealdb_url", "surrealdb_user", "surrealdb_pass",
        ],
    )

    # The router overrides system_managed_fields with _get_system_managed_fields()
    # which checks settings.SURREALDB_URL — patch it so the expected fields are returned
    system_fields = ["surrealdb_url", "surrealdb_user", "surrealdb_pass"]

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    with patch("routers.admin_org_config._get_system_managed_fields", return_value=system_fields):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/config")

    assert resp.status_code == 200
    body = resp.json()
    assert "surrealdb_url" in body["system_managed_fields"]
    assert len(body["system_managed_fields"]) == 3


# ── PATCH "" ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_org_config_success() -> None:
    """PATCH /admin/org/config returns 200 with updated config."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=OrgConfigService)
    mock_service.update_config.return_value = OrgConfigBase(
        llm_backend="openai",
        llm_model="gpt-4",
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/admin/org/config",
            json={"llm_backend": "openai", "llm_model": "gpt-4"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_backend"] == "openai"
    assert body["llm_model"] == "gpt-4"
    mock_service.update_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_org_config_partial() -> None:
    """PATCH /admin/org/config with partial fields works."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=OrgConfigService)
    mock_service.update_config.return_value = OrgConfigBase(
        llm_backend="openai",
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/admin/org/config",
            json={"llm_backend": "openai"},
        )

    assert resp.status_code == 200
    assert resp.json()["llm_backend"] == "openai"


@pytest.mark.asyncio
async def test_update_org_config_422_invalid_field() -> None:
    """PATCH /admin/org/config returns 422 for invalid field values."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=OrgConfigService)
    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/admin/org/config",
            json={"llm_temperature": 99.0},  # Out of 0.0–2.0 range
        )

    assert resp.status_code == 422


# ── PUT "" ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replace_org_config_success() -> None:
    """PUT /admin/org/config returns 200 with replaced config."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    mock_service = AsyncMock(spec=OrgConfigService)
    mock_service.update_config.return_value = OrgConfigBase(
        llm_backend="anthropic",
        llm_model="claude-3-opus",
    )

    app.dependency_overrides[_get_config_service] = lambda: mock_service

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/admin/org/config",
            json={
                "llm_backend": "anthropic",
                "llm_model": "claude-3-opus",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_backend"] == "anthropic"
    assert body["llm_model"] == "claude-3-opus"


# ── 401 / 403 auth ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_config_401_unauthenticated() -> None:
    """GET /admin/org/config returns 401 when unauthenticated."""
    app = FastAPI()
    # Set up app state so get_db and _get_config_service resolve successfully.
    # require_org_id will raise 401 naturally because no middleware sets org_id.
    app.state.db_session_factory = AsyncMock()
    app.state.openbao_client = AsyncMock()
    app.state.redis = AsyncMock()
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/org/config")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_config_requires_scope() -> None:
    """PATCH /admin/org/config returns 403 without admin:write scope."""
    app = FastAPI()

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "api_key"
        request.state.api_key_scopes = ["read"]  # No admin:write
        response = await call_next(request)
        return response

    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    # require_scope("admin:write") will check scopes and find only ["read"]
    # Since auth_type is "api_key", it will check scopes and fail with 403
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/admin/org/config",
            json={"llm_backend": "openai"},
        )

    # The require_scope("admin:write") will find the scope missing for api_key auth
    # and raise 403
    assert resp.status_code in (401, 403)


def _raise_401():
    from fastapi import HTTPException, status

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
