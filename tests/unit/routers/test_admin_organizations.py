"""Unit tests for the admin organization config router.

Tests prompt template CRUD and custom instructions endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import (
    get_dashboard_user,
    require_org_admin,
    require_org_id,
)
from dependencies.db import get_db
from routers.admin_organizations import router
from schemas.custom_instructions import (
    CustomInstructionSchema,
    CustomInstructionsResponse,
    SetCustomInstructionsRequest,
)
from schemas.prompt_templates import (
    ImportPromptRequest,
    PromptTemplateDetail,
    PromptTemplateListResponse,
    PromptTemplateSummary,
    PromptTemplateVersionsResponse,
    SetPromptTemplateRequest,
    SystemPromptGroup,
    SystemPromptGroupsResponse,
    SystemTemplateEntry,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _create_app() -> tuple[FastAPI, AsyncMock]:
    """Build a minimal FastAPI app with the admin_organizations router."""
    app = FastAPI()
    db_mock = AsyncMock(spec=AsyncSession)

    # Auth middleware
    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        response = await call_next(request)
        return response

    app.dependency_overrides[get_db] = lambda: db_mock
    app.dependency_overrides[require_org_id] = lambda: str(ORG_ID)
    app.dependency_overrides[require_org_admin] = lambda: str(ORG_ID)
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)

    app.include_router(router)
    return app, db_mock


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Prompt Templates ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_prompt_templates_success() -> None:
    """GET /admin/org/prompts returns 200 with template summaries."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.list_names.return_value = [
            {
                "name": "extract_facts",
                "version": 2,
                "is_customised": True,
                "description": "Fact extraction prompt",
                "type": "fact_extraction",
                "is_default_for_type": True,
                "updated_at": _now(),
            },
            {
                "name": "summarize",
                "version": 1,
                "is_customised": False,
                "description": "Summary prompt",
                "type": "summarization",
                "is_default_for_type": False,
                "updated_at": _now(),
            },
        ]
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/prompts")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["data"][0]["name"] == "extract_facts"
    assert body["data"][1]["name"] == "summarize"
    repo_instance.list_names.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_list_system_prompts_success() -> None:
    """GET /admin/org/prompts/system returns 200 with grouped system prompts."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.list_system_grouped.return_value = [
            SystemPromptGroup(
                type="fact_extraction",
                templates=[
                    SystemTemplateEntry(
                        name="extract_facts_v2",
                        version=1,
                        type="fact_extraction",
                        is_active=True,
                        is_default_for_type=True,
                        is_system_default=True,
                        description="Default fact extraction",
                    ),
                ],
                imported=[],
            )
        ]
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/prompts/system")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["groups"]) == 1
    assert body["groups"][0]["type"] == "fact_extraction"
    repo_instance.list_system_grouped.assert_awaited_once_with(org_id=ORG_ID)


@pytest.mark.asyncio
async def test_import_system_prompt_success() -> None:
    """POST /admin/org/prompts/import returns 201 with template detail."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.import_system_template.return_value = _make_detail(
            name="extract_facts_v2",
            version=1,
            text="Extract all facts from the text.",
            desc="Default fact extraction",
            is_active=True,
        )
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/org/prompts/import",
                json={"template_name": "extract_facts_v2"},
            )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "extract_facts_v2"
    assert body["version"] == 1
    repo_instance.import_system_template.assert_awaited_once_with(
        org_id=ORG_ID,
        template_name="extract_facts_v2",
    )


@pytest.mark.asyncio
async def test_import_system_prompt_409_already_imported() -> None:
    """POST /admin/org/prompts/import returns 409 when already imported."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.import_system_template.side_effect = ValueError(
            "already imported: extract_facts_v2"
        )
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/org/prompts/import",
                json={"template_name": "extract_facts_v2"},
            )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_import_system_prompt_404_not_found() -> None:
    """POST /admin/org/prompts/import returns 404 when system default missing."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.import_system_template.side_effect = ValueError(
            "No active system default for 'missing_name'"
        )
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/org/prompts/import",
                json={"template_name": "missing_name"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_prompt_default_success() -> None:
    """POST /admin/org/prompts/{name}/set-default returns 200."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.set_as_type_default.return_value = _make_detail(
            name="extract_facts",
            version=2,
            text="Extract facts",
            desc="Fact extraction prompt",
            is_active=True,
            is_default_for_type=True,
        )
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/org/prompts/extract_facts/set-default")

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "extract_facts"
    assert body["is_default_for_type"] is True
    repo_instance.set_as_type_default.assert_awaited_once_with(
        org_id=ORG_ID, name="extract_facts"
    )


@pytest.mark.asyncio
async def test_get_prompt_template_success() -> None:
    """GET /admin/org/prompts/{name} returns 200 with template detail."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.get_active.return_value = _make_detail(
            name="extract_facts",
            version=2,
            text="Extract facts from text.",
            desc="Fact extraction prompt",
            is_active=True,
        )
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/prompts/extract_facts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "extract_facts"
    assert body["version"] == 2
    repo_instance.get_active.assert_awaited_once_with(ORG_ID, "extract_facts")


@pytest.mark.asyncio
async def test_get_prompt_template_404() -> None:
    """GET /admin/org/prompts/{name} returns 404 when not found."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.get_active.return_value = None
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/prompts/missing_name")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_list_prompt_versions_success() -> None:
    """GET /admin/org/prompts/{name}/versions returns 200 with version list."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        active = _make_detail(
            name="extract_facts",
            version=2,
            text="v2 text",
            desc="v2 desc",
            is_active=True,
        )
        v1 = _make_detail(
            name="extract_facts",
            version=1,
            text="v1 text",
            desc="v1 desc",
            is_active=False,
        )
        repo_instance.get_active.return_value = active
        repo_instance.list_versions.return_value = [active, v1]
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/prompts/extract_facts/versions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "extract_facts"
    assert body["current_version"] == 2
    assert len(body["versions"]) == 2
    repo_instance.get_active.assert_awaited_once_with(ORG_ID, "extract_facts")


@pytest.mark.asyncio
async def test_list_prompt_versions_404() -> None:
    """GET /admin/org/prompts/{name}/versions returns 404 when template missing."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.get_active.return_value = None
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/prompts/missing_name/versions")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_prompt_template_success() -> None:
    """PUT /admin/org/prompts/{name} returns 201 with new template version."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    # Need to set up app.state.redis so the cache-invalidate code path works
    app.state.redis = AsyncMock()
    app.state.redis.delete = AsyncMock()

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.set_for_org.return_value = _make_detail(
            name="custom_prompt",
            version=1,
            text="Custom prompt text here.",
            desc="My custom prompt",
            is_active=True,
        )
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                "/admin/org/prompts/custom_prompt",
                json={
                    "template_text": "Custom prompt text here.",
                    "description": "My custom prompt",
                    "type": "fact_extraction",
                },
            )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "custom_prompt"
    assert body["version"] == 1
    assert body["template_text"] == "Custom prompt text here."
    repo_instance.set_for_org.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_prompt_template_success() -> None:
    """POST /admin/org/prompts/{name}/rollback/{version} returns 200."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.rollback.return_value = _make_detail(
            name="extract_facts",
            version=3,
            text="Rolled back text",
            desc="Rolled back to v1",
            is_active=True,
        )
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/org/prompts/extract_facts/rollback/1"
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 3
    assert body["template_text"] == "Rolled back text"
    repo_instance.rollback.assert_awaited_once_with(
        org_id=ORG_ID, name="extract_facts", version=1
    )


@pytest.mark.asyncio
async def test_rollback_prompt_template_404() -> None:
    """POST /admin/org/prompts/{name}/rollback/{version} returns 404."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.rollback.side_effect = ValueError("Version 99 not found")
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/org/prompts/extract_facts/rollback/99"
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_prompt_template_success() -> None:
    """DELETE /admin/org/prompts/{name} returns 204."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        active = _make_detail(
            name="custom_prompt",
            version=1,
            text="text",
            desc="desc",
            is_active=True,
            is_default_for_type=False,
        )
        repo_instance.get_active.return_value = active
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/admin/org/prompts/custom_prompt")

    assert resp.status_code == 204
    repo_instance.delete_for_org.assert_awaited_once_with(
        org_id=ORG_ID, name="custom_prompt"
    )


@pytest.mark.asyncio
async def test_delete_prompt_template_404() -> None:
    """DELETE /admin/org/prompts/{name} returns 404 when not found."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.get_active.return_value = None
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/admin/org/prompts/missing_name")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_prompt_template_409_is_default() -> None:
    """DELETE /admin/org/prompts/{name} returns 409 when it's the type default."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch("routers.admin_organizations.PromptTemplateRepository") as repo_cls:
        repo_instance = AsyncMock()
        active = _make_detail(
            name="default_prompt",
            version=1,
            text="text",
            desc="desc",
            is_active=True,
            is_default_for_type=True,
        )
        repo_instance.get_active.return_value = active
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/admin/org/prompts/default_prompt")

    assert resp.status_code == 409


# ── Custom Instructions ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_custom_instructions_success() -> None:
    """GET /admin/org/custom-instructions returns 200 with instructions."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch(
        "repositories.custom_instruction_repository.CustomInstructionRepository"
    ) as repo_cls:
        repo_instance = AsyncMock()
        instruction = MagicMock()
        instruction.name = "legal_domain"
        instruction.text = "This is a legal domain instruction."
        repo_instance.get_by_scope.return_value = [instruction]
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/org/custom-instructions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "legal_domain"


@pytest.mark.asyncio
async def test_set_custom_instructions_success() -> None:
    """PUT /admin/org/custom-instructions returns 201."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch(
        "repositories.custom_instruction_repository.CustomInstructionRepository"
    ) as repo_cls:
        repo_instance = AsyncMock()
        instruction = MagicMock()
        instruction.name = "medical_domain"
        instruction.text = "Medical domain instructions."
        repo_instance.set_by_scope.return_value = [instruction]
        repo_cls.return_value = repo_instance

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
    repo_instance.set_by_scope.assert_awaited_once()


@pytest.mark.asyncio
async def test_clear_custom_instructions_success() -> None:
    """DELETE /admin/org/custom-instructions returns 204."""
    app, db_mock = _create_app()
    transport = ASGITransport(app=app)

    with patch(
        "repositories.custom_instruction_repository.CustomInstructionRepository"
    ) as repo_cls:
        repo_instance = AsyncMock()
        repo_instance.set_by_scope.return_value = []
        repo_cls.return_value = repo_instance

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/admin/org/custom-instructions")

    assert resp.status_code == 204
    repo_instance.set_by_scope.assert_awaited_once_with(
        org_id=ORG_ID,
        scope="extraction",
        target_id=None,
        instructions=[],
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
