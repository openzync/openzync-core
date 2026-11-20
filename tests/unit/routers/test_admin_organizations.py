"""Unit tests for the admin organization config router.

Tests prompt template CRUD and custom instructions endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import SortSpec
from dependencies.auth import (
    get_dashboard_user,
    require_user_id,
)
from dependencies.db import get_db
from routers.admin_organizations import (
    get_custom_instruction_service,
    router,
)

USER_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(autouse=True)
def _stub_permission_gate() -> None:
    """Stub the permission gate for every test in this file.

    The router gates with ``require_permission("configuration:read")`` /
    ``require_permission("configuration:write")`` — closures created at
    router import time that cannot be keyed in ``dependency_overrides``.
    Patching ``dependencies.auth._check_permission`` (the shared decision
    function) stubs the gate while keeping the ``require_user_id`` chain
    intact.  The real gate matrix is covered by ``test_admin_gate_matrix.py``.
    """
    with patch("dependencies.auth._check_permission", new=AsyncMock()):
        yield


def _create_app() -> tuple[FastAPI, AsyncMock]:
    """Build a minimal FastAPI app with the admin_organizations router."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)

    # Auth middleware
    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.user_id = str(USER_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_user_id] = lambda: str(USER_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)

    app.include_router(router)
    return app, db_mock


def _now() -> datetime:
    return datetime.now(UTC)


# ── Custom Instructions ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_custom_instructions_success() -> None:
    """GET /admin/org/custom-instructions returns 200 with instructions."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    fake_service = AsyncMock()
    instruction = MagicMock()
    instruction.name = "legal_domain"
    instruction.text = "This is a legal domain instruction."
    fake_service.list_for_scope.return_value = [instruction]
    app.dependency_overrides[get_custom_instruction_service] = (
        lambda: fake_service
    )

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/org/custom-instructions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "legal_domain"
    fake_service.list_for_scope.assert_awaited_once_with(
        user_id=USER_ID,
        scope="extraction",
    )


@pytest.mark.asyncio
async def test_set_custom_instructions_success() -> None:
    """PUT /admin/org/custom-instructions returns 201."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    fake_service = AsyncMock()
    instruction = MagicMock()
    instruction.name = "medical_domain"
    instruction.text = "Medical domain instructions."
    fake_service.set_for_scope.return_value = [instruction]
    app.dependency_overrides[get_custom_instruction_service] = (
        lambda: fake_service
    )

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/admin/org/custom-instructions",
            json={
                "instructions": [
                    {
                        "name": "medical_domain",
                        "text": "Medical domain instructions.",
                    }
                ]
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "medical_domain"
    fake_service.set_for_scope.assert_awaited_once()


@pytest.mark.asyncio
async def test_clear_custom_instructions_success() -> None:
    """DELETE /admin/org/custom-instructions returns 204."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    fake_service = AsyncMock()
    fake_service.clear_for_scope.return_value = None
    app.dependency_overrides[get_custom_instruction_service] = (
        lambda: fake_service
    )

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/admin/org/custom-instructions")

    assert resp.status_code == 204
    fake_service.clear_for_scope.assert_awaited_once_with(
        user_id=USER_ID,
        scope="extraction",
    )


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _make_detail(
    name: str,
    version: int,
    text: str,
    desc: str | None,
    is_active: bool = True,
    is_default_for_type: bool = False,
    type_: str | None = None,
) -> MagicMock:
    """Create a mock ORM-like object that PromptTemplateDetail can validate.

    The schema's ``model_validator`` converts ORM objects to dicts before
    validation, so we use a MagicMock with the right attributes.
    """
    obj = MagicMock()
    obj.template_name = name
    obj.name = name
    obj.version = version
    obj.template_text = text
    obj.description = desc
    obj.type = type_
    obj.is_active = is_active
    obj.is_default_for_type = is_default_for_type
    obj.is_system_default = False
    return obj
